from __future__ import annotations

import argparse
import asyncio
import contextlib
import math
import pathlib
import sys
import time
from dataclasses import dataclass

import can
import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from lib.axis_sync import SyncGroup
from lib.can_manager import CANManager
from lib.config_schema import RobotConfig, load_robot_config, load_system_config
from lib.control.position_loop import M3508PositionLoop, make_position_pid
from lib.control.trajectory import TrapezoidalProfile
from lib.drivers.base import ControlMode
from lib.drivers.m3508 import CURRENT_MAX, M3508Driver
from lib.sequence.positions import AxisSpec, MotionSpec, load_position_table
from lib.tuning.metrics import Sample, analyze_step_response, settle_band_for, step_span

SAMPLE_INTERVAL_S = 0.005
SETTLE_RATIO = 0.02
FEEDBACK_WAIT_S = 3.0
MOTION_DWELL_MARGIN_S = 0.3


@dataclass(frozen=True)
class MotorTrace:
    name: str
    samples: list[Sample]
    metrics: object | None
    peak_current: float = 0.0
    saturation_ratio: float = 0.0


@dataclass(frozen=True)
class StepResult:
    target: float
    motors: list[MotorTrace]
    peak_deviation: float
    final_deviation: float
    aborted: str | None = None


class StepRunner:
    def __init__(
        self,
        loop: M3508PositionLoop,
        drivers: dict[str, M3508Driver],
        spec: AxisSpec,
        group: SyncGroup,
    ) -> None:
        self._loop = loop
        self._drivers = drivers
        self._spec = spec
        self._group = group

    def observed_value(self) -> float:
        return self._spec.to_value(self._commands())

    def deviation(self) -> float | None:
        return self._group.deviation(self._commands())

    def _commands(self) -> dict[str, float]:
        return {name: d.multi_turn_position for name, d in self._drivers.items()}

    def _member(self, name: str):
        return next(m for m in self._spec.motors if m.name == name)

    def _dead_band_value(self, name: str) -> float:
        return self._loop.pid(name).dead_band / abs(self._member(name).scale)

    async def step(self, target_value: float, dwell_s: float) -> StepResult:
        commands = self._spec.to_commands(target_value)
        traces: dict[str, list[Sample]] = {name: [] for name in self._drivers}
        peak_currents: dict[str, float] = dict.fromkeys(self._drivers, 0.0)
        saturated_counts: dict[str, int] = dict.fromkeys(self._drivers, 0)
        peak_deviation = 0.0
        aborted: str | None = None

        started = time.monotonic()
        for name, command in commands.items():
            await self._loop.set_target(name, ControlMode.POSITION, command)

        while True:
            now = time.monotonic()
            elapsed = now - started
            if elapsed >= dwell_s:
                break

            positions = self._commands()
            deviation = self._group.deviation(positions)
            if deviation is not None:
                peak_deviation = max(peak_deviation, abs(deviation))

            for name, driver in self._drivers.items():
                member = self._member(name)
                peak_currents[name] = max(peak_currents[name], abs(driver.state.current))
                saturated = self._loop.is_saturated(name)
                saturated_counts[name] += int(saturated)
                traces[name].append(
                    Sample(
                        t=elapsed,
                        target=member.to_value(commands[name]),
                        position=member.to_value(driver.multi_turn_position),
                        output=self._loop.pid(name).last_output,
                        saturated=saturated,
                    )
                )

            if self._loop.sync_violations:
                aborted = f"左右のずれが許容 ({self._group.tolerance}) を超えました"
                break

            await asyncio.sleep(SAMPLE_INTERVAL_S)

        final = self.deviation()
        return StepResult(
            target=target_value,
            motors=[
                MotorTrace(
                    name,
                    samples,
                    _analyze(samples, self._dead_band_value(name)),
                    peak_currents[name],
                    saturated_counts[name] / len(samples) if samples else 0.0,
                )
                for name, samples in traces.items()
            ],
            peak_deviation=peak_deviation,
            final_deviation=abs(final) if final is not None else 0.0,
            aborted=aborted,
        )


def _analyze(samples: list[Sample], dead_band_value: float):
    span = step_span(samples)
    if span is None:
        return None
    step_size = span[1] - span[0]
    band = settle_band_for(step_size, ratio=SETTLE_RATIO, minimum=abs(dead_band_value))
    return analyze_step_response(samples, settle_band=band)


def _format_metrics(trace: MotorTrace, unit: str) -> str:
    sat = f"飽和 {trace.saturation_ratio * 100:.0f}%"
    m = trace.metrics
    if m is None:
        return f"    {trace.name}: 解析できるサンプルがありません ({sat})"

    def opt(value, digits=2, suffix=""):
        return "—" if value is None else f"{value:.{digits}f}{suffix}"

    return (
        f"    {trace.name}: "
        f"立上り {opt(m.rise_time_s, 3, 's')} / "
        f"行き過ぎ {m.overshoot_pct:.1f}% / "
        f"整定 {opt(m.settling_time_s, 3, 's')} / "
        f"定常偏差 {m.steady_state_error:+.3f}{unit} / "
        f"{sat} / "
        f"指令 {m.peak_output:.0f} → 実電流 {trace.peak_current:.0f} counts"
    )


@dataclass(frozen=True)
class TrialConfig:
    kp: float
    ki: float
    kd: float
    sync_kp: float
    output_limit: float
    motion: MotionSpec | None = None

    def label(self) -> str:
        parts = [f"kp={self.kp:g}"]
        if self.ki:
            parts.append(f"ki={self.ki:g}")
        if self.kd:
            parts.append(f"kd={self.kd:g}")
        parts.append(f"sync_kp={self.sync_kp:g}")
        parts.append(f"olim={self.output_limit:g}")
        if self.motion is not None:
            parts.append(f"v={self.motion.max_velocity:g}")
            parts.append(f"a={self.motion.max_acceleration:g}")
            parts.append(f"vff={self.motion.velocity_ff:g}")
        return " ".join(parts)


def _build_profile(motion: MotionSpec, scale: float) -> TrapezoidalProfile:
    factor = abs(scale)
    return TrapezoidalProfile(
        max_velocity=motion.max_velocity * factor,
        max_acceleration=motion.max_acceleration * factor,
    )


def _describe_profile(spec: AxisSpec, motion: MotionSpec) -> str:
    per_motor = " / ".join(
        f"{m.name} v<={motion.max_velocity * abs(m.scale):.1f} "
        f"a<={motion.max_acceleration * abs(m.scale):.1f}"
        for m in spec.motors
    )
    return (
        f"プロファイル: v<={motion.max_velocity:g}{spec.unit}/s "
        f"a<={motion.max_acceleration:g}{spec.unit}/s^2 "
        f"velocity_ff={motion.velocity_ff:g} "
        f"[{spec.command_unit} 換算: {per_motor}]"
    )


async def _wait_for_feedback(drivers: dict[str, M3508Driver], can_manager: CANManager) -> None:
    deadline = time.monotonic() + FEEDBACK_WAIT_S
    while time.monotonic() < deadline:
        missing = [n for n in drivers if can_manager.last_feedback_at(n) is None]
        if not missing:
            return
        await asyncio.sleep(0.05)
    missing = [n for n in drivers if can_manager.last_feedback_at(n) is None]
    raise SystemExit(
        f"フィードバックが届いていないモータがあります: {', '.join(missing)}。"
        " candump で 0x201 / 0x202 が流れているか確認してください"
    )


async def _run_trial(
    runner: StepRunner,
    trial: TrialConfig,
    loop: M3508PositionLoop,
    spec: AxisSpec,
    *,
    amplitude: float,
    cycles: int,
    dwell_s: float,
) -> list[StepResult]:
    origin = runner.observed_value()

    manual = spec.manual
    peak = origin + amplitude
    if manual is not None and manual.clamp(peak) != peak:
        raise SystemExit(
            f"到達点 {peak:+.2f} (起点 {origin:+.2f} + 振幅 {amplitude}) が"
            f" 可動範囲 ({manual.min_value}〜{manual.max_value}) の外です。"
            " 軸を範囲の中ほどへ戻してから再実行してください"
        )
    results: list[StepResult] = []
    print(f"\n=== {trial.label()} (起点 {origin:+.2f}{spec.unit}) ===")
    if trial.motion is not None:
        print(f"  {_describe_profile(spec, trial.motion)}")

    for _ in range(cycles):
        for target in (origin + amplitude, origin):
            result = await runner.step(target, dwell_s)
            results.append(result)
            print(
                f"  ステップ → {target:+.2f}{spec.unit}  "
                f"左右ずれ 最大 {result.peak_deviation:.3f}{spec.unit} / "
                f"終了時 {result.final_deviation:.3f}{spec.unit}"
            )
            for trace in result.motors:
                print(_format_metrics(trace, spec.unit))
            if result.aborted:
                print(f"  !! 中止: {result.aborted}")
                return results
    return results


def _print_comparison(trials: list[tuple[TrialConfig, list[StepResult]]], unit: str) -> None:
    print("\n================ 比較 ================")
    print(f"{'条件':<46} {'ずれ最大':>10} {'ずれ平均':>10} {'整定(代表)':>12} {'飽和':>7}")
    for trial, results in trials:
        if not results:
            continue
        peak = max(r.peak_deviation for r in results)
        mean = sum(r.peak_deviation for r in results) / len(results)
        settles = [
            m.metrics.settling_time_s
            for r in results
            for m in r.motors
            if m.metrics is not None and m.metrics.settling_time_s is not None
        ]
        saturations = [m.saturation_ratio for r in results for m in r.motors]
        settle_text = f"{sum(settles) / len(settles):.3f}s" if settles else "—"
        sat_text = f"{max(saturations) * 100:.0f}%" if saturations else "—"
        print(
            f"{trial.label():<46} {peak:>9.3f}{unit} {mean:>9.3f}{unit} "
            f"{settle_text:>12} {sat_text:>7}"
        )


def _load_yaml(path: pathlib.Path) -> dict:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _floats(text: str) -> list[float]:
    return [float(part) for part in text.split(",") if part.strip()]


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="左右ペア軸のステップ応答を実測する (機体が動く)")
    parser.add_argument("--config", default="config/bench/y_axis_tuning/main_hand.yaml")
    parser.add_argument("--system", default="config/bench/y_axis_tuning/system.yaml")
    parser.add_argument("--positions", default=None)
    parser.add_argument("--axis", default="y_axis")
    parser.add_argument("--amplitude", type=float, default=1.0, help="ステップ幅 (人間の単位)")
    parser.add_argument("--cycles", type=int, default=1, help="往復回数")
    parser.add_argument("--dwell", type=float, default=1.5, help="1 ステップの記録時間 [s]")
    parser.add_argument("--kp", default=None, help="kp (カンマ区切りでスイープ)")
    parser.add_argument("--ki", default=None, help="ki")
    parser.add_argument("--kd", default=None, help="kd")
    parser.add_argument("--sync-kp", default=None, help="同期補正ゲイン (カンマ区切りでスイープ)")
    parser.add_argument(
        "--output-limit",
        default=None,
        help="電流指令の上限 [counts] (カンマ区切りでスイープ)",
    )
    parser.add_argument(
        "--max-velocity",
        default=None,
        help="台形プロファイルの巡航速度 [単位/s] (カンマ区切りでスイープ)",
    )
    parser.add_argument(
        "--max-acceleration",
        default=None,
        help="台形プロファイルの加減速度 [単位/s^2] (カンマ区切りでスイープ)",
    )
    parser.add_argument(
        "--velocity-ff",
        default=None,
        help="参照速度に掛けて feedforward へ足す係数 (kd と同じ単位。カンマ区切りでスイープ)",
    )
    return parser.parse_args(argv)


def _build_trials(
    args: argparse.Namespace, base: dict[str, float], base_motion: MotionSpec | None
) -> list[TrialConfig]:
    kps = _floats(args.kp) if args.kp else [base["kp"]]
    kis = _floats(args.ki) if args.ki else [base["ki"]]
    kds = _floats(args.kd) if args.kd else [base["kd"]]
    syncs = _floats(args.sync_kp) if args.sync_kp else [base["sync_kp"]]
    output_limits = [
        min(abs(v), float(CURRENT_MAX))
        for v in (_floats(args.output_limit) if args.output_limit else [base["output_limit"]])
    ]

    velocities = _floats(args.max_velocity) if args.max_velocity else _base_list(base_motion, "v")
    accelerations = (
        _floats(args.max_acceleration) if args.max_acceleration else _base_list(base_motion, "a")
    )
    ffs = _floats(args.velocity_ff) if args.velocity_ff else _base_list(base_motion, "ff")

    if bool(velocities) != bool(accelerations):
        raise SystemExit(
            "--max-velocity と --max-acceleration は対で指定してください"
            " (片方だけでは台形プロファイルを組み立てられません)"
        )
    if not velocities and args.velocity_ff:
        raise SystemExit(
            "--velocity-ff はプロファイルを有効にしたときだけ効きます。"
            " --max-velocity と --max-acceleration も指定してください"
        )
    if not ffs:
        ffs = [0.0]

    sweeping = [
        name
        for name, values in (
            ("kp", kps),
            ("ki", kis),
            ("kd", kds),
            ("sync_kp", syncs),
            ("output_limit", output_limits),
            ("max_velocity", velocities),
            ("max_acceleration", accelerations),
            ("velocity_ff", ffs),
        )
        if len(values) > 1
    ]
    if len(sweeping) > 1:
        raise SystemExit(f"同時にスイープできるのは 1 つだけです: {', '.join(sweeping)}")

    motions = _build_motions(velocities, accelerations, ffs)

    trials = []
    for kp in kps:
        for ki in kis:
            for kd in kds:
                for sync_kp in syncs:
                    for output_limit in output_limits:
                        for motion in motions:
                            trials.append(
                                TrialConfig(
                                    kp=kp,
                                    ki=ki,
                                    kd=kd,
                                    sync_kp=sync_kp,
                                    output_limit=output_limit,
                                    motion=motion,
                                )
                            )
    return trials


def _base_list(base_motion: MotionSpec | None, key: str) -> list[float]:
    if base_motion is None:
        return []
    return [
        {
            "v": base_motion.max_velocity,
            "a": base_motion.max_acceleration,
            "ff": base_motion.velocity_ff,
        }[key]
    ]


def _build_motions(
    velocities: list[float], accelerations: list[float], ffs: list[float]
) -> list[MotionSpec | None]:
    if not velocities:
        return [None]
    try:
        return [
            MotionSpec(max_velocity=v, max_acceleration=a, velocity_ff=ff)
            for v in velocities
            for a in accelerations
            for ff in ffs
        ]
    except ValueError as exc:
        raise SystemExit(f"プロファイルの値が不正です: {exc}") from exc


def _resolve_bus_alias(robot: RobotConfig, spec: AxisSpec) -> str:
    missing = [name for name in spec.motor_names if name not in robot.motors]
    if missing:
        raise SystemExit(
            f"軸 '{spec.name}' のモータ {', '.join(missing)} が robot config"
            f" ({robot.robot_name}) にありません。--config と --positions が"
            " 同じ機体のものか確認してください"
        )

    buses = {name: robot.motors[name].bus for name in spec.motor_names}
    aliases = set(buses.values())
    if len(aliases) > 1:
        detail = " / ".join(f"{name}={alias}" for name, alias in buses.items())
        raise SystemExit(
            f"軸 '{spec.name}' のモータが別のバスに分かれています ({detail})。"
            " 左右を同じフレームで同時に指令できないので、config を見直してください"
        )
    return buses[spec.motor_names[0]]


def _check_dwell(trials: list[TrialConfig], *, amplitude: float, dwell_s: float) -> None:
    for trial in trials:
        if trial.motion is None:
            continue
        travel_s = trial.motion.duration_for(amplitude)
        required = travel_s + MOTION_DWELL_MARGIN_S
        if dwell_s < required:
            raise SystemExit(
                f"--dwell {dwell_s}s では {trial.label()} の移動 "
                f"({travel_s:.2f}s) を記録しきれません。"
                f" --dwell {math.ceil(required * 10.0) / 10.0:.1f} 以上を指定してください"
            )


async def _main_async(args: argparse.Namespace) -> int:
    config_path = pathlib.Path(args.config)
    robot = load_robot_config(_load_yaml(config_path), source=str(config_path))
    system = load_system_config(_load_yaml(pathlib.Path(args.system)), source=args.system)
    positions_path = (
        pathlib.Path(args.positions)
        if args.positions
        else config_path.parent / f"{robot.robot_name}_positions.yaml"
    )
    table = load_position_table(_load_yaml(positions_path), source=str(positions_path))

    spec = table.axis(args.axis)
    base_group = spec.sync_group
    if base_group is None:
        raise SystemExit(f"軸 '{args.axis}' に sync_tolerance がありません")

    manual = spec.manual
    if manual is not None and manual.clamp(args.amplitude) != args.amplitude:
        raise SystemExit(
            f"振幅 {args.amplitude} が manual の可動範囲 "
            f"({manual.min_value}〜{manual.max_value}) の外です。"
            " 範囲を広げるなら先に実測すること (scripts/sync_probe.py)"
        )

    can_manager = CANManager()
    bus_alias = _resolve_bus_alias(robot, spec)
    if bus_alias not in system.can_buses:
        raise SystemExit(
            f"バス別名 '{bus_alias}' が --system の can_buses にありません"
            f" (定義済みなのは {', '.join(system.can_buses)})"
        )
    channel = system.can_buses[bus_alias]
    can_manager.add_bus(bus_alias, can.Bus(interface="socketcan", channel=channel))

    scales = {m.name: m.scale for m in spec.motors}

    drivers: dict[str, M3508Driver] = {}
    for name in spec.motor_names:
        cfg = robot.motors[name]
        driver = M3508Driver(name, can_id=cfg.can_id)
        drivers[name] = driver
        can_manager.add_motor(bus_alias, driver)

    base_pid = robot.motors[spec.motor_names[0]].pid
    if not base_pid:
        raise SystemExit(
            f"モータ '{spec.motor_names[0]}' に pid セクションがありません。"
            " 既定値をこのツールが持つと config と二重管理になるので、config へ明示すること"
        )
    missing = [k for k in ("kp", "ki", "kd", "dead_band", "output_limit") if k not in base_pid]
    if missing:
        raise SystemExit(
            f"pid セクションに {', '.join(missing)} がありません (config へ明示すること)"
        )
    base = {
        "kp": float(base_pid["kp"]),
        "ki": float(base_pid["ki"]),
        "kd": float(base_pid["kd"]),
        "sync_kp": base_group.sync_kp,
        "output_limit": float(base_pid["output_limit"]),
    }
    dead_band = float(base_pid["dead_band"])
    raw_integral_limit = base_pid.get("integral_limit")
    integral_limit = None if raw_integral_limit is None else float(raw_integral_limit)
    trials = _build_trials(args, base, spec.motion)
    _check_dwell(trials, amplitude=args.amplitude, dwell_s=args.dwell)

    if any(t.ki for t in trials) and integral_limit is None:
        raise SystemExit(
            "ki を入れる試行があるのに integral_limit が null です。"
            " config の pid.integral_limit に出力寄与の上限 [counts] を設定してください"
        )

    print(f"--- {args.axis} のステップ応答を実測 ---")
    print(f"  バス       : {channel}")
    print(f"  振幅       : {args.amplitude}{spec.unit} x {args.cycles} 往復")
    limits = sorted({t.output_limit for t in trials})
    print(f"  出力上限   : {', '.join(f'{v:.0f}' for v in limits)} counts")
    if len(limits) > 1 and integral_limit is not None:
        print(
            f"  ** integral_limit ({integral_limit:.0f}) と sync_limit は固定です。"
            "出力上限に対する割合が試行ごとに変わります **"
        )
    print(f"  sync_tolerance: {base_group.tolerance}{spec.unit}")
    if trials[0].motion is None:
        print("  プロファイル: なし (最終目標をステップで入れる)")
    else:
        print(f"  {_describe_profile(spec, trials[0].motion)}")
    print(f"  試行       : {len(trials)} 通り")
    print("  ** 機体が動きます。可動範囲から離れてください **")

    def build_loop(trial: TrialConfig) -> tuple[M3508PositionLoop, SyncGroup]:
        loop = M3508PositionLoop(can_manager, bus_alias)
        for name, driver in drivers.items():
            pid = make_position_pid(
                trial.kp,
                trial.ki,
                trial.kd,
                integral_limit=integral_limit,
                dead_band=dead_band,
            )
            pid.output_min = -trial.output_limit
            pid.output_max = trial.output_limit
            loop.add_motor(name, driver, pid)
            if trial.motion is not None:
                loop.set_motion_profile(
                    name,
                    _build_profile(trial.motion, scales[name]),
                    velocity_ff=trial.motion.velocity_ff,
                )
        group = SyncGroup(
            name=base_group.name,
            members=base_group.members,
            tolerance=base_group.tolerance,
            sync_kp=trial.sync_kp,
            sync_limit=base_group.sync_limit if trial.sync_kp else None,
        )
        loop.add_sync_group(group)
        return loop, group

    completed: list[tuple[TrialConfig, list[StepResult]]] = []
    position_loop, _ = build_loop(trials[0])
    await can_manager.run()
    try:
        await _wait_for_feedback(drivers, can_manager)
        runner = StepRunner(position_loop, drivers, spec, base_group)

        initial = runner.deviation()
        if initial is None:
            raise SystemExit("左右の位置を比較できません (フィードバックが揃っていない)")
        if abs(initial) > base_group.tolerance / 2.0:
            raise SystemExit(
                f"開始前の左右ずれが大きすぎます ({initial:.3f}{spec.unit})。"
                " 機構を揃えてから実行してください"
            )
        print(f"  開始前のずれ: {initial:+.3f}{spec.unit}\n")

        for trial in trials:
            position_loop, group = build_loop(trial)
            runner = StepRunner(position_loop, drivers, spec, group)

            position_loop.start()
            try:
                results = await _run_trial(
                    runner,
                    trial,
                    position_loop,
                    spec,
                    amplitude=args.amplitude,
                    cycles=args.cycles,
                    dwell_s=args.dwell,
                )
            finally:
                for name in spec.motor_names:
                    position_loop.clear_target(name)
                await position_loop.stop()
            completed.append((trial, results))
            if results and results[-1].aborted:
                break
    finally:
        with contextlib.suppress(Exception):
            await position_loop.send_stop_frame()
        await can_manager.shutdown()

    _print_comparison(completed, spec.unit)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        return asyncio.run(_main_async(args))
    except KeyboardInterrupt:
        print("\n中断しました (電流 0 を送って終了)")
        return 130


if __name__ == "__main__":
    sys.exit(main())
