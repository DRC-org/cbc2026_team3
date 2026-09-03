"""左右直結ペア軸のステップ応答を実測して PID / 同期補正を詰める。

**機体を動かす。** 実行すると指定した振幅で軸が往復する。人が可動範囲の外に居て、
非常停止に手が届くことを確認してから使うこと。

**shebang は持たない (実行属性も付けない)。** 理由は scripts/edulite_set_id.py と同じ。

--- なぜ main.py + UI ではなくこれを使うか -------------------------------
UI の /pid-tuning でもステップ応答は見られるが、調整は「同じ条件で値だけを変えて
繰り返し、前と比べる」作業である。手で操作すると振幅も待ち時間も毎回わずかに違い、
その差が指標の差と区別できない。ここは目標の入れ方・待ち時間・記録の窓を固定し、
**ゲインだけを変えた同一条件の試行**を並べて出す。

制御そのものは本番と同じ ``M3508PositionLoop`` が行う。ゲインと台形速度プロファイルの
上書きはプロセス内に閉じるので config は書き換わらない —— 良い値が決まってから
config へ書き戻す (途中の値が残らない)。

--- 調整するのは PID ゲインだけではない ------------------------------------
位置定数に ``axes.<軸>.motion`` を書いた軸は、最終目標ではなく速度・加速度で
制限した中間目標が PID へ入る。したがってつまみは 3 つ増える:

  ``max_velocity``     巡航速度 [mm/s]
  ``max_acceleration`` 加減速度 [mm/s^2]
  ``velocity_ff``      参照速度に掛けて feedforward へ足す係数

**``motion`` を持つ config なら既定でそれを使う** (本番と同じ制御になる)。
持たない config で ``--max-velocity`` / ``--max-acceleration`` を渡せば、その場だけ
プロファイルを有効にできる。どちらも無ければ従来どおりのステップ入力。

--- 安全機構 ---------------------------------------------------------------
本番の保護のうち、機構を守る層はそのまま効く:
  - フィードバック途絶でグループ全員を電流 0 (``SyncGuard``)
  - 左右のずれが ``sync_tolerance`` を超えたら電流 0 にラッチ (同上)
このスクリプトはそれに加えて、試行の前後で以下を確認する:
  - 開始前: 両モータのフィードバックが届いていること / 初期のずれが小さいこと
  - 試行中: 偏差ラッチが立ったら即座に中止して電流 0
  - 終了時: 必ず目標を解除し、0 電流フレームを送ってからバスを閉じる

--- 飽和を最初に見る --------------------------------------------------------
出力が上限に張り付いている間、**ゲインを変えても応答は変わらない**。飽和率を見ずに
kp を振ると「上げても下げても同じ」という観察から抜け出せない。試行ごとに
「飽和した周期の割合」を必ず出すので、まずそこを読むこと (プロファイルを有効にした
なら、飽和はプロファイルが機構の能力を超えている合図でもある)。

使い方:
    # まず一番小さい振幅で 1 往復 (現状のゲインの確認)
    uv run python scripts/tune_y_axis.py --amplitude 0.5 --cycles 1

    # kp をスイープして比べる
    uv run python scripts/tune_y_axis.py --amplitude 2.0 --kp 2,4,8

    # 良い kp が決まったら同期補正を入れて比べる
    uv run python scripts/tune_y_axis.py --amplitude 2.0 --kp 8 --sync-kp 0,2

--- 実運用ストローク (5〜15mm) の詰め方 --------------------------------------
**振幅 1.5mm までしか実測していない。** 大きな移動は挙動が別物になるので、
必ず次の順で上げる。順番を飛ばすと、どれが効いたのか読めない結果だけが残る。
**手順の全文と各段の中止条件は ``docs/mechanism_handoff.md`` §3-2。**

  0. 可動範囲を実測してから始める (``scripts/sync_probe.py`` を無励磁で走らせ、
     手で端から端まで動かす)。``--amplitude`` は ``manual`` の可動範囲でしか
     通らないので、実運用の振幅を出すには位置定数を本番へ差し替える:
       --positions config/main_hand_positions.yaml
     **``--config`` は本番の config/main_hand.yaml でもベンチ側でもよい** ——
     開くのは ``--axis`` の軸が載っているバス 1 本だけなので、3 本のバスを持つ
     本番 config でも m3508_bus に決まる。**ただしベンチ側 (config/bench/
     y_axis_tuning/) の output_limit は 800 で本番は 2000** (飽和の境界が
     0.45mm と 1.14mm で 2.5 倍違う) ので、ベンチ側の robot config で実運用振幅を
     測るなら本番と同じ値へ上げること

  1. 小さい振幅で現状を確認する (プロファイルが効いていることの確認)
       --amplitude 1.5 --dwell 1.5

  2. 実運用の振幅へ広げる。**ここで飽和率が跳ね上がるなら a_max か v_max が
     機構の能力を超えている。**ゲインではなくそちらを下げる
       --amplitude 15 --dwell 3.0

  3. ``a_max`` を上げる (立ち上がりが速くなる。行き過ぎと飽和率を見る)
       --amplitude 15 --dwell 3.0 --max-acceleration 300,600,1000

  4. 最後に ``v_max`` を上げる。**velocity_ff と kd をセットで見直すこと。**
     ``kd`` の単位は counts/(deg/s) なので、巡航速度がそのまま制動として出力に
     乗る —— kd=1.0 では 50mm/s = 2750deg/s が D 項だけで -2750counts になり、
     ``output_limit`` 2000 を超えて**逆向きに飽和する**。定常追従では実測速度が
     参照速度にほぼ等しいので、参照速度へ kd と同じ係数 (velocity_ff = kd) を
     掛けて足せばその制動をちょうど打ち消せる。**片方だけ動かすと巡航中に
     D 項が出力を食い潰し、飽和率だけが上がって速くならない**
       --amplitude 15 --dwell 3.0 --max-velocity 50,80
       --amplitude 15 --dwell 3.0 --max-velocity 80 --velocity-ff 0,1.0,1.6
"""

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

#: 記録の間隔 [s]。制御周期 (200Hz) と同じにする。これより粗いと行き過ぎのピークを
#: 取り逃し、細かくしても制御周期より速い変化は存在しない
SAMPLE_INTERVAL_S = 0.005
#: 整定帯をステップ幅の何割に取るか (制御工学の慣習値)
SETTLE_RATIO = 0.02
#: 開始前にフィードバックの到着を待つ時間 [s]
FEEDBACK_WAIT_S = 3.0
#: プロファイルの所要時間に対して記録窓へ最低限残す余白 [s]。
#: 窓が移動の途中で閉じると、行き過ぎも整定も**まだ起きていない**ものを測ることに
#: なり、指標が一律に良い方へ嘘をつく (行き過ぎは減速が終わってから出る)
MOTION_DWELL_MARGIN_S = 0.3


@dataclass(frozen=True)
class MotorTrace:
    """1 モータ分の記録と指標。"""

    name: str
    samples: list[Sample]
    metrics: object | None
    #: C620 が返した実電流の最大絶対値 [counts]。指令と同じスケールなので直接比べられる。
    #: **指令が出ているのに動かないとき、原因を 2 つに割れる唯一の値** ——
    #: 実電流が指令に追いていれば「電流は流れているのに回らない」= 機構側、
    #: ほぼ 0 なら「そもそも電流が出ていない」= C620・配線・電源側
    peak_current: float = 0.0
    #: 出力が上限に張り付いていた周期の割合 (0.0〜1.0)。**記録から直接数える。**
    #: ``StepMetrics.saturation_ratio`` と同じ値だが、そちらは「ステップと呼べる
    #: 入力が無かった」応答で None になる —— 動かなかった試行こそ飽和を知りたいので、
    #: 指標が出せたかどうかに依存しない経路で持つ
    saturation_ratio: float = 0.0


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

    def _member(self, name: str):
        return next(m for m in self._spec.motors if m.name == name)

    def _dead_band_value(self, name: str) -> float:
        """PID の不感帯を**人間の単位**へ戻す。

        ``PIDController.dead_band`` はモータの指令単位 (M3508 ならモータ軸 deg) で
        持っている。整定帯は人間の単位 (mm) のサンプルに対して使うので、換算せずに
        渡すと帯が scale 倍だけ広くなる —— y_axis では 1.0deg がそのまま 1.0mm の
        帯として効き、**0.5mm のステップが最初から帯の中に入って「整定 0.000s」**
        になる (実際には 1mm も動いていないのに)。
        幅なので符号は落とす。
        """
        return self._loop.pid(name).dead_band / abs(self._member(name).scale)

    async def step(self, target_value: float, dwell_s: float) -> StepResult:
        """``target_value`` へのステップを入れ、``dwell_s`` のあいだ記録する。"""
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
                # 偏差ラッチが立った = グループ全員が電流 0 になっている。
                # このまま次のステップへ進んでも力が入らないので必ず止める
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
    """指標を出す。**帯の下限には不感帯を (人間の単位で) 渡す。**

    不感帯の内側では偏差が 0 として扱われて制御が働かないので、それより狭い帯で
    「整定していない」と判定すると、正常な機構が永久に整定しない応答として出る。
    逆に指令単位のまま渡すと帯が scale 倍に広がり、動いていない応答が
    「即座に整定した」と出る (StepRunner._dead_band_value を参照)。
    """
    span = step_span(samples)
    if span is None:
        return None
    step_size = span[1] - span[0]
    band = settle_band_for(step_size, ratio=SETTLE_RATIO, minimum=abs(dead_band_value))
    return analyze_step_response(samples, settle_band=band)


def _format_metrics(trace: MotorTrace, unit: str) -> str:
    """1 モータ分の 1 行。**飽和率は指標が出せなかった応答でも必ず出す。**

    飽和している間はゲインを変えても応答が変わらないので、これが読めないと
    「上げても下げても同じ」という観察から抜け出せない。動かなかった試行
    (指標が None になる) こそその状態でありうるため、指標とは別経路で持つ。
    """
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
    """1 試行の条件。ゲインと台形速度プロファイルの両方を持つ。

    ``motion`` が None なら**その試行だけ**従来どおり最終目標をステップで入れる。
    値は軸の人間の単位のまま持ち、指令単位への換算は ``_build_profile`` が行う
    (単位換算を 1 箇所へ閉じる)。
    """

    kp: float
    ki: float
    kd: float
    sync_kp: float
    motion: MotionSpec | None = None

    def label(self) -> str:
        parts = [f"kp={self.kp:g}"]
        if self.ki:
            parts.append(f"ki={self.ki:g}")
        if self.kd:
            parts.append(f"kd={self.kd:g}")
        parts.append(f"sync_kp={self.sync_kp:g}")
        if self.motion is not None:
            parts.append(f"v={self.motion.max_velocity:g}")
            parts.append(f"a={self.motion.max_acceleration:g}")
            parts.append(f"vff={self.motion.velocity_ff:g}")
        return " ".join(parts)


def _build_profile(motion: MotionSpec, scale: float) -> TrapezoidalProfile:
    """人間の単位の制限を**モータの指令単位**へ換算したプロファイルを作る。

    ``main._attach_motion_profiles`` と同じ換算。``abs(scale)`` で掛けるのは、
    速度・加速度の制限が向きを持たない量だから —— 逆回転ペアは ``scale`` の符号が
    逆なので、符号付きで掛けると片側の上限が負値になる (``TrapezoidalProfile`` は
    正の上限しか受け取らないため、そこで落ちる)。
    """
    factor = abs(scale)
    return TrapezoidalProfile(
        max_velocity=motion.max_velocity * factor,
        max_acceleration=motion.max_acceleration * factor,
    )


def _describe_profile(spec: AxisSpec, motion: MotionSpec) -> str:
    """プロファイルの制限を指令単位まで展開した 1 行。

    人間の単位のまま出すと、``scale`` の取り違え (符号・桁) が画面のどこにも
    現れない。実際にループへ渡る値を出す。
    """
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

    # **起点は試行ごとにドリフトする** (定常偏差が残るので、往復しても完全には
    # 戻らない)。振幅だけを可動範囲と照合しても、起点が寄っていけば到達点は
    # 範囲外へ出る。動かす直前の実測起点で毎回見る
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
    """試行を横に並べる。**比較こそがこのツールの目的。**"""
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
        # 飽和は記録から直接数えた値を使う。指標 (StepMetrics) 経由にすると、
        # 動かなかった試行 —— まさに飽和を疑うべき試行 —— で欄が「—」になる
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
    """スイープするパラメータは 1 つだけ許す。

    2 つ同時に振ると組み合わせの数だけ機体が動き、しかもどちらが効いたのか
    読めない結果が並ぶ。**プロファイルのつまみ (v_max / a_max / velocity_ff) も
    同じ 1 本の制限に入れる** —— ゲインと軌道を同時に振ったら、なおさら読めない。

    ``base_motion`` は位置定数の ``axes.<軸>.motion``。書いてあればそれを既定に
    使う (本番と同じ制御で測る)。書いていない config でも
    ``--max-velocity`` / ``--max-acceleration`` を対で渡せばその場だけ有効にできる。
    """
    kps = _floats(args.kp) if args.kp else [base["kp"]]
    kis = _floats(args.ki) if args.ki else [base["ki"]]
    kds = _floats(args.kd) if args.kd else [base["kd"]]
    syncs = _floats(args.sync_kp) if args.sync_kp else [base["sync_kp"]]

    # 空リスト = 「この試行にプロファイルは無い」。既定値で埋めない ——
    # 埋めると motion を書いていない軸に勝手な制限が掛かり、config と実機の
    # 挙動が食い違う (しかも画面からは読めない)
    velocities = _floats(args.max_velocity) if args.max_velocity else _base_list(base_motion, "v")
    accelerations = (
        _floats(args.max_acceleration) if args.max_acceleration else _base_list(base_motion, "a")
    )
    ffs = _floats(args.velocity_ff) if args.velocity_ff else _base_list(base_motion, "ff")

    if bool(velocities) != bool(accelerations):
        # 片方だけでは軌道を組み立てられない。既定値で補うと「書いたのに効かない
        # 制限」になる (位置定数の MotionSpec と同じ方針)
        raise SystemExit(
            "--max-velocity と --max-acceleration は対で指定してください"
            " (片方だけでは台形プロファイルを組み立てられません)"
        )
    if not velocities and args.velocity_ff:
        # プロファイルが無ければ参照速度が存在しないので、この係数は黙って
        # 効かない値になる
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
                    for motion in motions:
                        trials.append(
                            TrialConfig(kp=kp, ki=ki, kd=kd, sync_kp=sync_kp, motion=motion)
                        )
    return trials


def _base_list(base_motion: MotionSpec | None, key: str) -> list[float]:
    """位置定数の ``motion`` を既定値の 1 要素リストにする。無ければ空。"""
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
    """組み合わせを ``MotionSpec`` にする。値の検証はその ``__post_init__`` に委ねる。

    検証をここへ書き写すと、位置定数の側と片方だけ直したときに気付けない
    (「config では拒否されるのにツールでは通る値」が作れる)。
    """
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
    """対象軸のモータが載っているバス別名を返す。**開くのはこの 1 本だけ。**

    このツールは 1 つの軸を詰めるものなので、その軸が使わないバスを開く理由が無い。
    モータ構成に現れるバスの集合から選ぶ実装だと、複数バスを持つ config
    (本番は 3 本、config/bench/m3508_edulite/ は 2 本) で開くバスが実行ごとに変わり、
    対象軸の載っていないバスを開いた回はフィードバックが 1 通も届かない ——
    症状は「同じコマンドなのに動いたり動かなかったりする」だけになる。
    """
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
        # C620 の電流指令フレーム (0x200) は 1 通で同一バス上の 4 モータへ届く。
        # 左右が別バスだと同時に指令できず、このツールの前提 (1 つのループが
        # ペアを束ねる) が崩れる。黙って片方のバスを開くと、開かれなかった側は
        # 力が入らないまま偏差だけが開く
        detail = " / ".join(f"{name}={alias}" for name, alias in buses.items())
        raise SystemExit(
            f"軸 '{spec.name}' のモータが別のバスに分かれています ({detail})。"
            " 左右を同じフレームで同時に指令できないので、config を見直してください"
        )
    return buses[spec.motor_names[0]]


def _check_dwell(trials: list[TrialConfig], *, amplitude: float, dwell_s: float) -> None:
    """記録窓がプロファイルの移動を含みきれるか確かめる。

    プロファイルを入れると移動そのものに時間が掛かる (振幅 15mm / v=50 / a=300 なら
    0.47 秒)。窓が移動の途中で閉じると、行き過ぎも整定も**まだ起きていない**ものを
    測ることになり、指標が一律に良い方へ嘘をつく。しかも「速いプロファイルほど
    数字が良い」という逆向きの結論が出るので、気付く手掛かりが無い。
    """
    for trial in trials:
        if trial.motion is None:
            continue
        travel_s = trial.motion.duration_for(amplitude)
        required = travel_s + MOTION_DWELL_MARGIN_S
        if dwell_s < required:
            # 提案値は必ず切り上げる。表示のために丸めて下げると、言われたとおりに
            # 打ち直しても同じ拒否が返る
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
        # 可動範囲は「この軸を動かしてよい範囲」の唯一の宣言なので、そこを
        # 超える振幅を歯止め無しに通さない。判定は ManualSpec.clamp に委ねる
        # (境界の解釈をここへ書き写すと、片方だけ直したときに気付けない)
        raise SystemExit(
            f"振幅 {args.amplitude} が manual の可動範囲 "
            f"({manual.min_value}〜{manual.max_value}) の外です。"
            " 範囲を広げるなら先に実測すること (scripts/sync_probe.py)"
        )

    # --- 配線 (本番と同じクラスを使う) ---
    can_manager = CANManager()
    bus_alias = _resolve_bus_alias(robot, spec)
    if bus_alias not in system.can_buses:
        raise SystemExit(
            f"バス別名 '{bus_alias}' が --system の can_buses にありません"
            f" (定義済みなのは {', '.join(system.can_buses)})"
        )
    channel = system.can_buses[bus_alias]
    can_manager.add_bus(bus_alias, can.Bus(interface="socketcan", channel=channel))

    # 単位換算 (人間の単位 → 指令単位) を知るのはこの層だけ。位置制御ループへは
    # 指令単位で渡す
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
    }
    dead_band = float(base_pid["dead_band"])
    output_limit = min(abs(float(base_pid["output_limit"])), float(CURRENT_MAX))
    raw_integral_limit = base_pid.get("integral_limit")
    integral_limit = None if raw_integral_limit is None else float(raw_integral_limit)
    trials = _build_trials(args, base, spec.motion)
    _check_dwell(trials, amplitude=args.amplitude, dwell_s=args.dwell)

    if any(t.ki for t in trials) and integral_limit is None:
        # 上限の無い積分は、機構端に当たって動けない間に際限なく育ち、拘束が
        # 外れた瞬間に暴走する。**ki を振るなら config で上限を宣言させる**
        raise SystemExit(
            "ki を入れる試行があるのに integral_limit が null です。"
            " config の pid.integral_limit に出力寄与の上限 [counts] を設定してください"
        )

    print(f"--- {args.axis} のステップ応答を実測 ---")
    print(f"  バス       : {channel}")
    print(f"  振幅       : {args.amplitude}{spec.unit} x {args.cycles} 往復")
    print(f"  出力上限   : {output_limit:.0f} counts")
    print(f"  sync_tolerance: {base_group.tolerance}{spec.unit}")
    # プロファイルの有無は試行全体で共通 (スイープしても値が変わるだけ)。
    # 「効いているつもり」で結果を読ませないため、無い場合も明示する ——
    # 最終目標がそのまま PID へ入る軸では、大きな移動は飽和したまま加速する
    if trials[0].motion is None:
        print("  プロファイル: なし (最終目標をステップで入れる)")
    else:
        print(f"  {_describe_profile(spec, trials[0].motion)}")
    print(f"  試行       : {len(trials)} 通り")
    print("  ** 機体が動きます。可動範囲から離れてください **")

    def build_loop(trial: TrialConfig) -> tuple[M3508PositionLoop, SyncGroup]:
        """試行ごとにループを作り直す。

        ゲインだけを差し替えて使い回すと、前の試行で育った積分や偏差ラッチが
        次の試行へ持ち越される。**同一条件で比べるのがこのツールの目的**なので、
        状態ごと作り直す。同期グループは frozen なのでここで組み立てる。
        プロファイルも位置と速度の状態を持つので、同じ理由で試行ごとに作り直す。
        """
        loop = M3508PositionLoop(can_manager, bus_alias)
        for name, driver in drivers.items():
            pid = make_position_pid(
                trial.kp,
                trial.ki,
                trial.kd,
                integral_limit=integral_limit,
                dead_band=dead_band,
            )
            pid.output_min = -output_limit
            pid.output_max = output_limit
            loop.add_motor(name, driver, pid)
            if trial.motion is not None:
                # 本番 (main._attach_motion_profiles) と同じく後付けで渡す。
                # 起点の実測は位置制御ループが最初の指令で行うので、ここで
                # フィードバック未受信の 0.0 が軌道の起点に焼き付くことはない
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
