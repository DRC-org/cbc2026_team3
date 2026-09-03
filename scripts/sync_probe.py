"""左右直結ペア軸のずれを受動的に観測する (PID / 同期補正のチューニング用)。

**このスクリプトは CAN へ 1 通も送信しない。** 制御プログラム (main.py) が動いて
いる最中に別プロセスで起動し、同じフィードバックを横から読むために作ってある。
送信しないので 0x200 の奪い合いも起きない。

**shebang は持たない (実行属性も付けない)。** python-can と lib/ を import するので
プロジェクトの venv でしか動かず、`uv run python scripts/sync_probe.py` としてしか
呼ばれない。理由は scripts/edulite_set_id.py の冒頭と同じ。

--- なぜ専用ツールが要るか -------------------------------------------------
同期偏差は WS 配信 (20Hz) の `manual.axes[].deviation` に**瞬時値**としてしか出ず、
しかも手動操縦パネルの軸行にしか現れない。y_axis の移動は数百 ms で終わるので、
20Hz では数サンプルしか映らず「**移動中に最大どれだけ開いたか**」を読めない。
補正の効果は移動中のピークにしか現れないので、それが見えないまま sync_kp を
上げ下げしても効いているのかどうか分からない。

このツールは C620 が 1kHz で流すフィードバックをそのまま読むのでピークを取り逃さず、
静止 → 移動 → 静止 の 1 区間を自動で切り出して要約する。

--- 読める値と読めない値 ---------------------------------------------------
偏差の算出は ``SyncGroup.deviation()`` をそのまま呼ぶ。逆換算をここへ書き写すと、
逆回転ペアの符号を落とした「別の値」を見ながらゲインを決めることになる。

**絶対位置は制御プログラム側と一致しない。** M3508Driver は最初に受け取った
フィードバックを累積角の原点にするので、このツールの原点は「ツールを起動した瞬間の
姿勢」である。したがって表示される位置は起動時からの相対値で、偏差も同じだけ
オフセットを持ちうる。**チューニングで見たいのは移動中の偏差の変化量**なので
これで足りるが、絶対的なずれ量として読んではならない。

使い方:
    # 既定 (config/main_hand.yaml の y_axis) を観測。Ctrl-C で終了
    uv run python scripts/sync_probe.py

    # 波形も残す (後から解析する / 補正あり・なしを比べる)
    uv run python scripts/sync_probe.py --csv /tmp/y_axis_before.csv
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import pathlib
import sys
import time
from dataclasses import dataclass, field

import can
import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from lib.axis_sync import SyncGroup
from lib.config_schema import load_robot_config, load_system_config
from lib.drivers.m3508 import M3508Driver
from lib.sequence.positions import AxisSpec, load_position_table

#: 移動とみなす速さ [unit/s]。エンコーダ 1 カウントのジッタ (y_axis で約 0.8mm/s
#: 相当) を移動と読まない値に置く。実際の移動は桁が 1 つ上になる
DEFAULT_STILL_SPEED = 2.0
#: 静止がこの時間続いたら区間を閉じる [s]
DEFAULT_STILL_S = 0.15
#: これ未満しか動かなかった区間は報告しない [unit]。振動やノイズを移動として
#: 数え上げると、要約が流れて肝心の移動が読めなくなる
DEFAULT_MIN_TRAVEL = 0.5


@dataclass(frozen=True)
class MoveSummary:
    """静止 → 移動 → 静止 の 1 区間の要約。"""

    index: int
    duration_s: float
    start_value: float
    end_value: float
    peak_deviation: float
    peak_at_value: float
    mean_abs_deviation: float
    samples: int

    @property
    def travel(self) -> float:
        return self.end_value - self.start_value


class MoveTracker:
    """位置の系列から「1 回の移動」を切り出し、その間の偏差のピークを保持する。

    チューニングで欲しいのは静止時の値ではなく**移動中の最大ずれ**である。
    区間の切り出しを人間の目に任せると、20Hz の画面を見ながら数百 ms のピークを
    読むことになる (読めない)。

    区間の開始は「動き始める直前のサンプル」に取る。速さが閾値を超えた時点の値を
    起点にすると、その 1 サンプル分だけ移動量が短く出る。
    """

    def __init__(
        self,
        *,
        still_speed: float = DEFAULT_STILL_SPEED,
        still_s: float = DEFAULT_STILL_S,
        min_travel: float = DEFAULT_MIN_TRAVEL,
    ) -> None:
        self._still_speed = still_speed
        self._still_s = still_s
        self._min_travel = min_travel

        self._index = 0
        self._prev: tuple[float, float] | None = None
        self._active = False
        self._start_time = 0.0
        self._start_value = 0.0
        self._last_motion_at = 0.0
        self._peak = 0.0
        self._peak_at = 0.0
        self._sum_abs = 0.0
        self._samples = 0

    @property
    def completed(self) -> int:
        """報告済みの移動回数。"""
        return self._index

    def observe(self, now: float, value: float, deviation: float | None) -> MoveSummary | None:
        """1 サンプルを取り込み、区間が閉じたらその要約を返す。

        Args:
            now: 単調時刻 [s]
            value: 軸の位置 (人間の単位)
            deviation: 同じ瞬間の左右ずれ。比較対象が揃わなければ None
        """
        prev = self._prev
        self._prev = (now, value)
        if prev is None:
            return None

        dt = now - prev[0]
        if dt <= 0.0:
            return None
        moving = abs(value - prev[1]) / dt >= self._still_speed

        if moving and not self._active:
            self._begin(prev[0], prev[1])
        if not self._active:
            return None
        if moving:
            self._last_motion_at = now

        self._accumulate(value, deviation)

        if not moving and now - self._last_motion_at >= self._still_s:
            return self._end(now, value)
        return None

    def _begin(self, at: float, value: float) -> None:
        self._active = True
        self._start_time = at
        self._start_value = value
        self._last_motion_at = at
        self._peak = 0.0
        self._peak_at = value
        self._sum_abs = 0.0
        self._samples = 0

    def _accumulate(self, value: float, deviation: float | None) -> None:
        if deviation is None:
            return
        magnitude = abs(deviation)
        if magnitude > self._peak:
            self._peak = magnitude
            self._peak_at = value
        self._sum_abs += magnitude
        self._samples += 1

    def _end(self, now: float, value: float) -> MoveSummary | None:
        self._active = False
        if abs(value - self._start_value) < self._min_travel:
            return None
        self._index += 1
        return MoveSummary(
            index=self._index,
            duration_s=self._last_motion_at - self._start_time,
            start_value=self._start_value,
            end_value=value,
            peak_deviation=self._peak,
            peak_at_value=self._peak_at,
            mean_abs_deviation=self._sum_abs / self._samples if self._samples else 0.0,
            samples=self._samples,
        )


@dataclass
class Observation:
    """観測の蓄積。**呼び出し側が持つ。**

    Ctrl-C は受信ループの中で上がるので、ループ側で所有するとそれまでの観測が
    全部消える (チューニングは「動かして Ctrl-C で止める」の繰り返しなので、
    毎回消えては使いものにならない)。

    位置の範囲を持つのは、**機構のストロークを実測する用途**があるため。
    M3508 は電流指令 0 の間まったく保持力を持たないので、制御プログラムを
    起動せずにこのツールだけを回し、機構を手で端から端まで動かせば、
    可動範囲がそのまま range として読める (1 通も送らないので安全)。
    """

    moves: list[MoveSummary] = field(default_factory=list)
    min_value: float | None = None
    max_value: float | None = None
    peak_deviation: float = 0.0
    samples: int = 0

    def record(self, value: float, deviation: float | None) -> None:
        self.samples += 1
        if self.min_value is None or value < self.min_value:
            self.min_value = value
        if self.max_value is None or value > self.max_value:
            self.max_value = value
        if deviation is not None and abs(deviation) > self.peak_deviation:
            self.peak_deviation = abs(deviation)

    @property
    def span(self) -> float | None:
        if self.min_value is None or self.max_value is None:
            return None
        return self.max_value - self.min_value


def _load_yaml(path: pathlib.Path) -> dict:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _positions_path(config: pathlib.Path, robot_name: str) -> pathlib.Path:
    return config.parent / f"{robot_name}_positions.yaml"


def _build_drivers(
    spec: AxisSpec, robot_motors: dict, axis: str
) -> tuple[dict[str, M3508Driver], str]:
    """軸を構成するモータのドライバを作り、載っているバス別名を返す。

    M3508 以外が混ざった軸は扱えない (このツールは C620 のフィードバックしか
    デコードしない)。黙って無視すると、片側だけを見た偏差を出してしまう。
    """
    drivers: dict[str, M3508Driver] = {}
    buses: set[str] = set()
    for name in spec.motor_names:
        motor = robot_motors.get(name)
        if motor is None:
            raise SystemExit(f"軸 '{axis}' のモータ '{name}' が robot config にありません")
        if motor.driver != "m3508":
            raise SystemExit(
                f"軸 '{axis}' のモータ '{name}' は driver={motor.driver} です "
                "(このツールは m3508 のフィードバックしか読めません)"
            )
        drivers[name] = M3508Driver(name, can_id=motor.can_id)
        buses.add(motor.bus)
    if len(buses) != 1:
        raise SystemExit(f"軸 '{axis}' のモータが複数のバスに分かれています: {sorted(buses)}")
    return drivers, buses.pop()


def _print_header(axis: str, spec: AxisSpec, group: SyncGroup, channel: str) -> None:
    print(f"--- {axis} を観測 (送信はしない) ---")
    print(f"  バス         : {channel}")
    print(f"  モータ       : {', '.join(spec.motor_names)}")
    print(f"  単位         : {spec.unit}")
    print(f"  sync_tolerance: {group.tolerance} {spec.unit}")
    print(f"  sync_kp       : {group.sync_kp} (0.0 なら補正なし)")
    if group.sync_limit is not None:
        print(f"  sync_limit    : {group.sync_limit} counts")
    print("  Ctrl-C で終了。軸を動かすと 1 移動ごとに要約が出ます")
    print()


def _format_summary(summary: MoveSummary, unit: str) -> str:
    return (
        f"[移動 {summary.index:2d}] "
        f"{summary.start_value:+8.2f} → {summary.end_value:+8.2f} {unit} "
        f"({summary.travel:+7.2f} {unit}, {summary.duration_s:.2f}s)  "
        f"ずれ最大 {summary.peak_deviation:6.3f} {unit} "
        f"@ {summary.peak_at_value:+7.2f} {unit}  "
        f"平均 {summary.mean_abs_deviation:6.3f} {unit}  "
        f"({summary.samples} サンプル)"
    )


def _run(
    bus: can.BusABC,
    drivers: dict[str, M3508Driver],
    spec: AxisSpec,
    group: SyncGroup,
    tracker: MoveTracker,
    writer: object | None,
    duration_s: float | None,
    observation: Observation,
) -> None:
    """受信ループ。**送信は 1 通も行わない。**

    ``observation`` は呼び出し側が持つ (理由は Observation の docstring)。
    """
    started = time.monotonic()
    seen: set[str] = set()

    while True:
        if duration_s is not None and time.monotonic() - started >= duration_s:
            return
        msg = bus.recv(timeout=0.5)
        if msg is None:
            continue
        for name, driver in drivers.items():
            if not driver.matches_feedback(msg):
                continue
            driver.update_state(msg)
            seen.add(name)
            break
        else:
            continue

        if len(seen) < len(drivers):
            # 片方しか受け取っていない間は偏差が意味を持たない
            continue

        now = time.monotonic()
        commands = {name: d.multi_turn_position for name, d in drivers.items()}
        value = spec.to_value(commands)
        deviation = group.deviation(commands)

        if writer is not None:
            row = [f"{now - started:.4f}", f"{value:.4f}"]
            row += [f"{commands[name]:.3f}" for name in spec.motor_names]
            row.append("" if deviation is None else f"{deviation:.5f}")
            writer.writerow(row)

        observation.record(value, deviation)
        summary = tracker.observe(now, value, deviation)
        if summary is not None:
            observation.moves.append(summary)
            print(_format_summary(summary, spec.unit), flush=True)


def _print_totals(observation: Observation, unit: str) -> None:
    print()
    if observation.samples == 0:
        print("フィードバックを 1 通も受け取りませんでした")
        return

    span = observation.span
    if span is not None:
        print("--- 観測した位置の範囲 (起動時の姿勢が 0) ---")
        print(f"  {observation.min_value:+.2f} 〜 {observation.max_value:+.2f} {unit}")
        print(f"  幅     : {span:.2f} {unit}")
    print(f"  ずれ最大 : {observation.peak_deviation:.3f} {unit} (静止中も含む全サンプル)")

    if not observation.moves:
        print("\n移動を 1 回も検出しませんでした")
        return
    peak = max(s.peak_deviation for s in observation.moves)
    mean = sum(s.mean_abs_deviation for s in observation.moves) / len(observation.moves)
    print(f"\n--- {len(observation.moves)} 回の移動 ---")
    print(f"  移動中のずれ最大 : {peak:.3f} {unit}")
    print(f"  移動中のずれ平均 : {mean:.3f} {unit}")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="左右直結ペア軸のずれを受動的に観測する (送信はしない)"
    )
    parser.add_argument("--config", default="config/main_hand.yaml", help="ロボット config")
    parser.add_argument("--system", default="config/system.yaml", help="system config")
    parser.add_argument("--positions", default=None, help="位置定数 (既定は config から導出)")
    parser.add_argument("--axis", default="y_axis", help="観測する軸名")
    parser.add_argument("--csv", default=None, help="波形の書き出し先")
    parser.add_argument("--duration", type=float, default=None, help="観測秒数 (既定は無制限)")
    parser.add_argument("--still-speed", type=float, default=DEFAULT_STILL_SPEED)
    parser.add_argument("--still-s", type=float, default=DEFAULT_STILL_S)
    parser.add_argument("--min-travel", type=float, default=DEFAULT_MIN_TRAVEL)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    config_path = pathlib.Path(args.config)
    robot = load_robot_config(_load_yaml(config_path), source=str(config_path))
    system = load_system_config(_load_yaml(pathlib.Path(args.system)), source=args.system)

    positions_path = (
        pathlib.Path(args.positions)
        if args.positions
        else _positions_path(config_path, robot.robot_name)
    )
    table = load_position_table(_load_yaml(positions_path), source=str(positions_path))

    if args.axis not in table.axes:
        raise SystemExit(f"軸 '{args.axis}' が {positions_path} にありません")
    spec = table.axis(args.axis)
    group = spec.sync_group
    if group is None:
        raise SystemExit(
            f"軸 '{args.axis}' に sync_tolerance がありません (左右ペア軸ではないので"
            "ずれという概念が無い)"
        )

    drivers, bus_alias = _build_drivers(spec, dict(robot.motors), args.axis)
    channel = system.can_buses.get(bus_alias)
    if channel is None:
        raise SystemExit(f"バス別名 '{bus_alias}' が {args.system} の can_buses にありません")

    tracker = MoveTracker(
        still_speed=args.still_speed, still_s=args.still_s, min_travel=args.min_travel
    )
    _print_header(args.axis, spec, group, channel)

    observation = Observation()
    with contextlib.ExitStack() as stack:
        writer = None
        if args.csv:
            csv_file = stack.enter_context(open(args.csv, "w", newline="", encoding="utf-8"))
            writer = csv.writer(csv_file)
            writer.writerow(
                ["t_s", f"axis_{spec.unit}", *spec.motor_names, f"deviation_{spec.unit}"]
            )

        # receive_own_messages=False。**このツールは send を 1 度も呼ばない**
        bus = can.Bus(interface="socketcan", channel=channel, receive_own_messages=False)
        stack.callback(bus.shutdown)
        with contextlib.suppress(KeyboardInterrupt):
            _run(bus, drivers, spec, group, tracker, writer, args.duration, observation)

    if args.csv:
        print(f"\n波形を書き出しました: {args.csv}")
    _print_totals(observation, spec.unit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
