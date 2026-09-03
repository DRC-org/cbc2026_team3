"""左右直結ペア軸のステップ応答を実測して PID / 同期補正を詰める。

**機体を動かす。** 実行すると指定した振幅で軸が往復する。人が可動範囲の外に居て、
非常停止に手が届くことを確認してから使うこと。

**shebang は持たない (実行属性も付けない)。** 理由は scripts/edulite_set_id.py と同じ。

--- なぜ main.py + UI ではなくこれを使うか -------------------------------
UI の /pid-tuning でもステップ応答は見られるが、調整は「同じ条件で値だけを変えて
繰り返し、前と比べる」作業である。手で操作すると振幅も待ち時間も毎回わずかに違い、
その差が指標の差と区別できない。ここは目標の入れ方・待ち時間・記録の窓を固定し、
**ゲインだけを変えた同一条件の試行**を並べて出す。

制御そのものは本番と同じ ``M3508PositionLoop`` が行う。ゲインの上書きは
プロセス内に閉じるので config は書き換わらない —— 良い値が決まってから
config へ書き戻す (途中の値が残らない)。

--- 安全機構 ---------------------------------------------------------------
本番の保護のうち、機構を守る層はそのまま効く:
  - フィードバック途絶でグループ全員を電流 0 (``SyncGuard``)
  - 左右のずれが ``sync_tolerance`` を超えたら電流 0 にラッチ (同上)
このスクリプトはそれに加えて、試行の前後で以下を確認する:
  - 開始前: 両モータのフィードバックが届いていること / 初期のずれが小さいこと
  - 試行中: 偏差ラッチが立ったら即座に中止して電流 0
  - 終了時: 必ず目標を解除し、0 電流フレームを送ってからバスを閉じる

使い方:
    # まず一番小さい振幅で 1 往復 (現状のゲインの確認)
    uv run python scripts/tune_y_axis.py --amplitude 0.5 --cycles 1

    # kp をスイープして比べる
    uv run python scripts/tune_y_axis.py --amplitude 2.0 --kp 2,4,8

    # 良い kp が決まったら同期補正を入れて比べる
    uv run python scripts/tune_y_axis.py --amplitude 2.0 --kp 8 --sync-kp 0,2
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import pathlib
import sys
import time
from dataclasses import dataclass

import can
import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from lib.axis_sync import SyncGroup
from lib.can_manager import CANManager
from lib.config_schema import load_robot_config, load_system_config
from lib.control.position_loop import M3508PositionLoop, make_position_pid
from lib.drivers.base import ControlMode
from lib.drivers.m3508 import CURRENT_MAX, M3508Driver
from lib.sequence.positions import AxisSpec, load_position_table
from lib.tuning.metrics import Sample, analyze_step_response, settle_band_for, step_span

#: 記録の間隔 [s]。制御周期 (200Hz) と同じにする。これより粗いと行き過ぎのピークを
#: 取り逃し、細かくしても制御周期より速い変化は存在しない
SAMPLE_INTERVAL_S = 0.005
#: 整定帯をステップ幅の何割に取るか (制御工学の慣習値)
SETTLE_RATIO = 0.02
#: 開始前にフィードバックの到着を待つ時間 [s]
FEEDBACK_WAIT_S = 3.0


@dataclass(frozen=True)
class MotorTrace:
    """1 モータ分の記録と指標。"""

    name: str
    samples: list[Sample]
    metrics: object | None


@dataclass(frozen=True)
class StepResult:
    """1 回のステップの結果 (軸全体)。"""

    target: float
    motors: list[MotorTrace]
    peak_deviation: float
    final_deviation: float
    aborted: str | None = None


class StepRunner:
    """目標を入れて記録し、指標にするところまで。

    ``M3508PositionLoop`` は 200Hz で自分の周期を回しているので、ここは
    **観測するだけ**で制御には一切関与しない (関与すると本番と違う挙動になる)。
    """

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
        """軸の現在位置 (人間の単位)。"""
        return self._spec.to_value(self._commands())

    def deviation(self) -> float | None:
        return self._group.deviation(self._commands())

    def _commands(self) -> dict[str, float]:
        return {name: d.multi_turn_position for name, d in self._drivers.items()}

    async def step(self, target_value: float, dwell_s: float) -> StepResult:
        """``target_value`` へのステップを入れ、``dwell_s`` のあいだ記録する。"""
        commands = self._spec.to_commands(target_value)
        traces: dict[str, list[Sample]] = {name: [] for name in self._drivers}
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
                member = next(m for m in self._spec.motors if m.name == name)
                traces[name].append(
                    Sample(
                        t=elapsed,
                        target=member.to_value(commands[name]),
                        position=member.to_value(driver.multi_turn_position),
                        output=self._loop.pid(name).last_output,
                        saturated=self._loop.is_saturated(name),
                    )
                )

            if self._loop.sync_violations:
                # 偏差ラッチが立った = グループ全員が電流 0 になっている。
                # このまま次のステップへ進んでも力が入らないので必ず止める
                aborted = f"左右のずれが許容 ({self._group.tolerance}) を超えました"
                break

            await asyncio.sleep(SAMPLE_INTERVAL_S)

        final = self.deviation()
        return StepResult(
            target=target_value,
            motors=[
                MotorTrace(name, samples, _analyze(samples, self._loop.pid(name).dead_band))
                for name, samples in traces.items()
            ],
            peak_deviation=peak_deviation,
            final_deviation=abs(final) if final is not None else 0.0,
            aborted=aborted,
        )


def _analyze(samples: list[Sample], dead_band_command: float):
    """指標を出す。**帯の下限には不感帯を渡す。**

    不感帯の内側では偏差が 0 として扱われて制御が働かないので、それより狭い帯で
    「整定していない」と判定すると、正常な機構が永久に整定しない応答として出る。
    """
    span = step_span(samples)
    if span is None:
        return None
    step_size = span[1] - span[0]
    band = settle_band_for(step_size, ratio=SETTLE_RATIO, minimum=abs(dead_band_command))
    return analyze_step_response(samples, settle_band=band)


def _format_metrics(trace: MotorTrace, unit: str) -> str:
    m = trace.metrics
    if m is None:
        return f"    {trace.name}: 解析できるサンプルがありません"

    def opt(value, digits=2, suffix=""):
        return "—" if value is None else f"{value:.{digits}f}{suffix}"

    return (
        f"    {trace.name}: "
        f"立上り {opt(m.rise_time_s, 3, 's')} / "
        f"行き過ぎ {m.overshoot_pct:.1f}% / "
        f"整定 {opt(m.settling_time_s, 3, 's')} / "
        f"定常偏差 {m.steady_state_error:+.3f}{unit} / "
        f"飽和 {m.saturation_ratio * 100:.0f}% / "
        f"最大出力 {m.peak_output:.0f}counts"
    )


@dataclass(frozen=True)
class TrialConfig:
    """1 試行のゲイン。"""

    kp: float
    ki: float
    kd: float
    sync_kp: float

    def label(self) -> str:
        parts = [f"kp={self.kp:g}"]
        if self.ki:
            parts.append(f"ki={self.ki:g}")
        if self.kd:
            parts.append(f"kd={self.kd:g}")
        parts.append(f"sync_kp={self.sync_kp:g}")
        return " ".join(parts)


async def _wait_for_feedback(drivers: dict[str, M3508Driver], can_manager: CANManager) -> None:
    """両モータのフィードバックが届くまで待つ。届かなければ動かさずに止める。

    未受信のまま目標を入れると、位置 0.0 を現在位置と信じて全ストロークぶんの
    指令を 1 回で出すことになる。
    """
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
    """1 つのゲインの組で往復させ、各ステップの結果を返す。"""
    origin = runner.observed_value()
    results: list[StepResult] = []
    print(f"\n=== {trial.label()} (起点 {origin:+.2f}{spec.unit}) ===")

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
    """試行を横に並べる。**比較こそがこのツールの目的。**"""
    print("\n================ 比較 ================")
    print(f"{'ゲイン':<28} {'ずれ最大':>10} {'ずれ平均':>10} {'整定(代表)':>12} {'飽和':>7}")
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
        saturations = [
            m.metrics.saturation_ratio for r in results for m in r.motors if m.metrics is not None
        ]
        settle_text = f"{sum(settles) / len(settles):.3f}s" if settles else "—"
        sat_text = f"{max(saturations) * 100:.0f}%" if saturations else "—"
        print(
            f"{trial.label():<28} {peak:>9.3f}{unit} {mean:>9.3f}{unit} "
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
    return parser.parse_args(argv)


def _build_trials(args: argparse.Namespace, base: dict[str, float]) -> list[TrialConfig]:
    """スイープするパラメータは 1 つだけ許す。

    2 つ同時に振ると組み合わせの数だけ機体が動き、しかもどちらが効いたのか
    読めない結果が並ぶ。
    """
    kps = _floats(args.kp) if args.kp else [base["kp"]]
    kis = _floats(args.ki) if args.ki else [base["ki"]]
    kds = _floats(args.kd) if args.kd else [base["kd"]]
    syncs = _floats(args.sync_kp) if args.sync_kp else [base["sync_kp"]]

    sweeping = [
        name
        for name, values in (("kp", kps), ("ki", kis), ("kd", kds), ("sync_kp", syncs))
        if len(values) > 1
    ]
    if len(sweeping) > 1:
        raise SystemExit(f"同時にスイープできるのは 1 つだけです: {', '.join(sweeping)}")

    trials = []
    for kp in kps:
        for ki in kis:
            for kd in kds:
                for sync_kp in syncs:
                    trials.append(TrialConfig(kp=kp, ki=ki, kd=kd, sync_kp=sync_kp))
    return trials


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
    if manual is not None and not (
        manual.min <= args.amplitude <= manual.max and manual.min <= -args.amplitude
    ):
        raise SystemExit(
            f"振幅 {args.amplitude} が manual の可動範囲 ({manual.min}〜{manual.max}) の外です。"
            " 範囲を広げるなら先に実測すること (scripts/sync_probe.py)"
        )

    # --- 配線 (本番と同じクラスを使う) ---
    can_manager = CANManager()
    bus_alias = next(iter({m.bus for m in robot.motors.values()}))
    channel = system.can_buses[bus_alias]
    can_manager.add_bus(bus_alias, can.Bus(interface="socketcan", channel=channel))

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
    }
    dead_band = float(base_pid["dead_band"])
    output_limit = min(abs(float(base_pid["output_limit"])), float(CURRENT_MAX))
    trials = _build_trials(args, base)

    print(f"--- {args.axis} のステップ応答を実測 ---")
    print(f"  バス       : {channel}")
    print(f"  振幅       : {args.amplitude}{spec.unit} x {args.cycles} 往復")
    print(f"  出力上限   : {output_limit:.0f} counts")
    print(f"  sync_tolerance: {base_group.tolerance}{spec.unit}")
    print(f"  試行       : {len(trials)} 通り")
    print("  ** 機体が動きます。可動範囲から離れてください **")

    def build_loop(trial: TrialConfig) -> tuple[M3508PositionLoop, SyncGroup]:
        """試行ごとにループを作り直す。

        ゲインだけを差し替えて使い回すと、前の試行で育った積分や偏差ラッチが
        次の試行へ持ち越される。**同一条件で比べるのがこのツールの目的**なので、
        状態ごと作り直す。同期グループは frozen なのでここで組み立てる。
        """
        loop = M3508PositionLoop(can_manager, bus_alias)
        for name, driver in drivers.items():
            pid = make_position_pid(trial.kp, trial.ki, trial.kd, dead_band=dead_band)
            pid.output_min = -output_limit
            pid.output_max = output_limit
            loop.add_motor(name, driver, pid)
        group = SyncGroup(
            name=base_group.name,
            members=base_group.members,
            tolerance=base_group.tolerance,
            sync_kp=trial.sync_kp,
            # sync_kp が 0 なら上限は要らない (SyncGroup が対で検証する)
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
