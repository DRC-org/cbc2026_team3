"""EDULITE 05 の生角度とパラメータを読むだけのツール。

電源を切って入れ直しても同じ生角度が返るか (= 機械ゼロが電源断で不動か) を
確かめるために使う。手順は docs/edulite_origin_probe.md。

**サーバー (main.py) と同時に走らせてはならない。** サーバーは起動時に
`set_zero_on_start: true` のモータへ SET_ZERO を送るので、確かめたい零点が
その場で書き換わり、確認そのものが成立しない。

既定で送るのは disable (フィードバック要求) と READ_PARAM だけで、どちらも
モータを励磁しない。SET_ZERO は --set-zero を明示したときにしか送らない。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import pathlib
import sys
import time
from dataclasses import dataclass
from datetime import datetime

import can
import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from lib.axis_sync import SyncGroup
from lib.config_schema import MotorConfig, load_robot_config, load_system_config
from lib.drivers.edulite05 import (
    Edulite05Driver,
    Edulite05Fault,
    Edulite05ModeState,
    Edulite05RunMode,
)
from lib.sequence.positions import AxisSpec, load_position_table

# 1 通の要求に対する応答待ち。EDULITE は要求から数 ms で返すので、これで足りなければ
# 応答が無いと判断してよい (待ち続けると「無応答」が「遅い」に化けて見えなくなる)
_RESPONSE_WINDOW_S = 0.05

# 他のプロセスがバスを叩いていないかを見るための受動観測。サーバーが動いていれば
# こちらが 1 通も送っていないのにフィードバックが流れてくる
_BUSY_LISTEN_S = 0.3

_READ_PARAMS: tuple[tuple[str, int], ...] = (
    ("run_mode", Edulite05Driver.PARAM_RUN_MODE),
    ("limit_spd", Edulite05Driver.PARAM_LIMIT_SPD),
    ("limit_cur", Edulite05Driver.PARAM_LIMIT_CUR),
    ("loc_kp", Edulite05Driver.PARAM_LOC_KP),
)

_UNREADABLE = "読めなかった (応答なし)"


@dataclass(frozen=True)
class MotorSample:
    position_rad: float
    mode_state: int | None
    fault_bits: Edulite05Fault


class ProbeError(RuntimeError):
    pass


def _load_yaml(path: pathlib.Path) -> dict:
    try:
        with path.open(encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except OSError as exc:
        raise SystemExit(f"設定ファイルを読めません: {path} ({exc})") from exc


def _positions_path(config: pathlib.Path, robot_name: str) -> pathlib.Path:
    return config.parent / f"{robot_name}_positions.yaml"


def _build_drivers(
    spec: AxisSpec, motors: dict[str, MotorConfig], axis: str
) -> tuple[dict[str, Edulite05Driver], str]:
    drivers: dict[str, Edulite05Driver] = {}
    buses: set[str] = set()
    for name in spec.motor_names:
        motor = motors.get(name)
        if motor is None:
            raise SystemExit(f"軸 '{axis}' のモータ '{name}' が robot config にありません")
        if motor.driver != "edulite05":
            raise SystemExit(
                f"軸 '{axis}' のモータ '{name}' は driver={motor.driver} です "
                "(このツールは edulite05 しか読めません)"
            )
        drivers[name] = Edulite05Driver(
            name=motor.name,
            can_id=motor.can_id,
            host_id=motor.host_id,
            mode=motor.mode,
            limit_speed=motor.limit_speed,
            limit_current=motor.limit_current,
            position_kp=motor.position_kp,
        )
        buses.add(motor.bus)
    if len(buses) != 1:
        raise SystemExit(f"軸 '{axis}' のモータが複数のバスに分かれています: {sorted(buses)}")
    return drivers, buses.pop()


def _open_bus(channel: str) -> can.BusABC:
    try:
        return can.Bus(interface="socketcan", channel=channel, receive_own_messages=False)
    except (OSError, can.CanError) as exc:
        raise SystemExit(
            f"CAN インタフェース '{channel}' を開けません ({exc})。"
            " scripts/setup_can.sh を実行してバスが up しているか確認してください"
        ) from exc


def _detect_other_talker(bus: can.BusABC, drivers: dict[str, Edulite05Driver]) -> str | None:
    deadline = time.monotonic() + _BUSY_LISTEN_S
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        msg = bus.recv(timeout=remaining)
        if msg is None:
            continue
        for name, driver in drivers.items():
            if driver.matches_feedback(msg):
                return name


def _drain(bus: can.BusABC) -> None:
    while bus.recv(timeout=0.0) is not None:
        pass


def _send(bus: can.BusABC, msg: can.Message) -> None:
    try:
        bus.send(msg)
    except can.CanError as exc:
        raise ProbeError(
            f"送信に失敗しました ({exc})。バスが落ちているか、ACK を返すノードが"
            " 1 台も居ません。scripts/setup_can.sh でバスを張り直し、"
            "モータに 24V が入っているか確かめてください"
            " (**本機は CAN トランシーバもモータ電源から取る**ので、"
            "無通電の個体はバス上に存在しません)"
        ) from exc


def _probe_motor(bus: can.BusABC, driver: Edulite05Driver) -> MotorSample | None:
    _send(bus, driver.feedback_probe_message())
    deadline = time.monotonic() + _RESPONSE_WINDOW_S
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        msg = bus.recv(timeout=remaining)
        if msg is None or not driver.matches_feedback(msg):
            continue
        state = driver.update_state(msg)
        return MotorSample(
            position_rad=state.position,
            mode_state=driver.mode_state,
            fault_bits=driver.fault_bits,
        )


def _read_param(bus: can.BusABC, driver: Edulite05Driver, param_id: int) -> float | int | None:
    _send(bus, driver.encode_read_param(param_id))
    deadline = time.monotonic() + _RESPONSE_WINDOW_S
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        msg = bus.recv(timeout=remaining)
        if msg is None or not driver.matches_read_param(msg):
            continue
        answered_id, value = driver.decode_read_param(msg)
        if answered_id == param_id:
            return value


def _collect_samples(
    bus: can.BusABC, drivers: dict[str, Edulite05Driver], count: int, interval_s: float
) -> list[dict[str, MotorSample | None]]:
    samples: list[dict[str, MotorSample | None]] = []
    for index in range(count):
        if index:
            time.sleep(interval_s)
        _drain(bus)
        samples.append({name: _probe_motor(bus, driver) for name, driver in drivers.items()})
    return samples


def _collect_params(
    bus: can.BusABC, drivers: dict[str, Edulite05Driver]
) -> dict[str, dict[str, float | int | None]]:
    params: dict[str, dict[str, float | int | None]] = {}
    for name, driver in drivers.items():
        _drain(bus)
        params[name] = {
            label: _read_param(bus, driver, param_id) for label, param_id in _READ_PARAMS
        }
    return params


def _mode_state_name(mode_state: int | None) -> str:
    if mode_state is None:
        return "不明"
    try:
        return Edulite05ModeState(mode_state).name
    except ValueError:
        return f"未知({mode_state})"


def _fault_name(fault: Edulite05Fault) -> str:
    # 未定義ビットが立つと name が None になるので、生ビットを出して「読めた」ことは残す
    return fault.name or f"0x{int(fault):02X}"


def _logical_deg(group: SyncGroup | None, spec: AxisSpec, name: str, position_rad: float) -> float:
    members = group.members if group is not None else spec.motors
    for member in members:
        if member.name == name:
            return member.to_value(position_rad)
    raise KeyError(name)


def _deviation(group: SyncGroup | None, sample: dict[str, MotorSample | None]) -> float | None:
    if group is None:
        return None
    positions = {name: motor.position_rad for name, motor in sample.items() if motor is not None}
    return group.deviation(positions)


def _print_header(
    args: argparse.Namespace,
    spec: AxisSpec,
    group: SyncGroup | None,
    drivers: dict[str, Edulite05Driver],
    channel: str,
    bus_alias: str,
    positions_path: pathlib.Path,
) -> None:
    print("=== EDULITE 05 生角度・パラメータ読み取り ===")
    if args.label:
        print(f"  ラベル       : {args.label}")
    print(f"  時刻         : {datetime.now().isoformat(timespec='seconds')}")
    print(f"  バス         : {channel} (別名 {bus_alias})")
    print(f"  軸           : {spec.name}  ({positions_path})")
    for name, driver in drivers.items():
        scale = _logical_deg(group, spec, name, 1.0)
        print(
            f"  モータ       : {name}  can_id=0x{driver.can_id:02X}"
            f"  host_id=0x{driver.host_id:02X}  1rad={scale:+.4f}{spec.unit}"
        )
    if group is not None:
        print(f"  sync_tolerance: {group.tolerance} {spec.unit}")
    sent = "disable (フィードバック要求)"
    if not args.no_read_param:
        sent += " と READ_PARAM"
    print(f"  送るフレーム : {sent}")
    print("                 励磁も位置指令も送らない")
    print()


def _print_samples(
    samples: list[dict[str, MotorSample | None]],
    spec: AxisSpec,
    group: SyncGroup | None,
) -> None:
    print(f"--- 位置フィードバック ({len(samples)} 回) ---")
    print("   #  モータ      生[rad]     生[deg]    論理[deg]   mode_state  fault")
    for index, sample in enumerate(samples, start=1):
        for name, motor in sample.items():
            if motor is None:
                print(f"  {index:2d}  {name:<10}  応答なし")
                continue
            print(
                f"  {index:2d}  {name:<10}"
                f"  {motor.position_rad:+10.6f}"
                f"  {math.degrees(motor.position_rad):+10.4f}"
                f"  {_logical_deg(group, spec, name, motor.position_rad):+10.4f}"
                f"  {_mode_state_name(motor.mode_state):<10}"
                f"  {_fault_name(motor.fault_bits)}"
            )
        deviation = _deviation(group, sample)
        if group is not None:
            shown = (
                "測れない (両輪の応答が揃わなかった)"
                if deviation is None
                else (f"{deviation:.4f} {spec.unit}")
            )
            print(f"      偏差: {shown}")
    print()


def _print_stats(
    samples: list[dict[str, MotorSample | None]],
    drivers: dict[str, Edulite05Driver],
    spec: AxisSpec,
    group: SyncGroup | None,
) -> None:
    print("--- まとめ ---")
    for name in drivers:
        values = [
            math.degrees(sample[name].position_rad)  # type: ignore[union-attr]
            for sample in samples
            if sample[name] is not None
        ]
        if not values:
            print(f"  {name:<10} 応答 0/{len(samples)}  (値は測れていない)")
            continue
        print(
            f"  {name:<10} 応答 {len(values)}/{len(samples)}"
            f"  生deg 平均 {sum(values) / len(values):+.4f}"
            f"  最小 {min(values):+.4f}  最大 {max(values):+.4f}"
            f"  ばらつき {max(values) - min(values):.4f}"
        )

    if group is None:
        return
    deviations = [d for d in (_deviation(group, sample) for sample in samples) if d is not None]
    if not deviations:
        print("  偏差       測れていない (両輪の応答が 1 度も揃わなかった)")
        return
    print(
        f"  偏差       平均 {sum(deviations) / len(deviations):.4f} {spec.unit}"
        f"  最小 {min(deviations):.4f}  最大 {max(deviations):.4f}"
        f"  (sync_tolerance {group.tolerance} {spec.unit})"
    )


def _format_param(label: str, value: float | int | None) -> str:
    if value is None:
        return _UNREADABLE
    if label == "run_mode":
        name = _run_mode_name(int(value))
        return f"{int(value)} ({name})"
    return f"{value:.4f}"


def _run_mode_name(value: int) -> str:
    try:
        return Edulite05RunMode(value).name
    except ValueError:
        return "未知"


def _print_params(params: dict[str, dict[str, float | int | None]]) -> None:
    print()
    print("--- パラメータ (READ_PARAM 0x11) ---")
    param_ids = dict(_READ_PARAMS)
    for name, values in params.items():
        for label, value in values.items():
            shown = _format_param(label, value)
            print(f"  {name:<10} {label:<10} (0x{param_ids[label]:04X}) = {shown}")


def _confirm_set_zero(drivers: dict[str, Edulite05Driver], assume_yes: bool) -> bool:
    print()
    print("!!! --set-zero を指定しています !!!")
    print("  SET_ZERO (通信タイプ 0x06) を次のモータへ送ります:")
    for name, driver in drivers.items():
        print(f"    {name} (can_id=0x{driver.can_id:02X})")
    print("  **今の姿勢がそのモータの原点として書き換わります。**")
    print("  書き換えた零点は電源断で失われる可能性があり、それを確かめるのがこの手順です。")
    if assume_yes:
        return True
    return input("  実行しますか? [y/N] ").strip().lower() in ("y", "yes")


def _send_set_zero(bus: can.BusABC, drivers: dict[str, Edulite05Driver]) -> None:
    for driver in drivers.values():
        # 励磁中に零点を切ると保持目標が飛ぶので、無励磁を確定させてから送る
        _send(bus, driver.encode_disable())
        time.sleep(0.05)
        _send(bus, driver.encode_set_zero())
        time.sleep(0.2)


def _print_no_response_hint() -> None:
    print()
    print("[!] 1 通も応答がありませんでした。次の順で切り分けてください:")
    print("    1. main.py が動いていないか (このツールと同時には走らせられません)")
    print("    2. scripts/setup_can.sh でバスを張り直す")
    print("    3. モータに 24V が入っているか")
    print("       (本機は CAN トランシーバもモータ電源から取るので、無通電なら応答しません)")
    print("    4. CAN の H/L・終端 120Ω・GND の共通化")


def _as_json(
    args: argparse.Namespace,
    spec: AxisSpec,
    group: SyncGroup | None,
    channel: str,
    samples: list[dict[str, MotorSample | None]],
    params: dict[str, dict[str, float | int | None]] | None,
) -> dict:
    return {
        "label": args.label,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "bus": channel,
        "axis": spec.name,
        "unit": spec.unit,
        "samples": [
            {
                "motors": {
                    name: None
                    if motor is None
                    else {
                        "position_rad": motor.position_rad,
                        "position_deg": math.degrees(motor.position_rad),
                        "logical_deg": _logical_deg(group, spec, name, motor.position_rad),
                        "mode_state": _mode_state_name(motor.mode_state),
                        "fault_bits": int(motor.fault_bits),
                    }
                    for name, motor in sample.items()
                },
                "deviation": _deviation(group, sample),
            }
            for sample in samples
        ],
        # 読めなかったパラメータは null。0 で埋めると「読めた 0」と区別できなくなる
        "params": params,
    }


def _measure(
    bus: can.BusABC,
    args: argparse.Namespace,
    spec: AxisSpec,
    group: SyncGroup | None,
    drivers: dict[str, Edulite05Driver],
) -> tuple[list[dict[str, MotorSample | None]], dict[str, dict[str, float | int | None]] | None]:
    samples = _collect_samples(bus, drivers, args.samples, args.interval)
    _print_samples(samples, spec, group)
    _print_stats(samples, drivers, spec, group)

    params = None if args.no_read_param else _collect_params(bus, drivers)
    if params is not None:
        _print_params(params)

    if all(motor is None for sample in samples for motor in sample.values()):
        _print_no_response_hint()
    return samples, params


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="EDULITE 05 の生角度・パラメータを読む (既定では動かすフレームを送らない)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--config", default="config/main_hand.yaml", help="ロボット config")
    parser.add_argument("--system", default="config/system.yaml", help="system config")
    parser.add_argument("--positions", default=None, help="位置定数 (既定は config から導出)")
    parser.add_argument("--axis", default="rotate", help="読む軸名")
    parser.add_argument("--samples", type=int, default=5, help="読む回数 (ばらつきを見る)")
    parser.add_argument("--interval", type=float, default=0.2, help="読む間隔 [s]")
    parser.add_argument("--label", default="", help="出力に付ける見出し (before / after など)")
    parser.add_argument("--json", default=None, help="読んだ値の書き出し先 (比較用)")
    parser.add_argument(
        "--no-read-param", action="store_true", help="READ_PARAM を送らず位置だけ読む"
    )
    parser.add_argument(
        "--set-zero",
        action="store_true",
        help="読み取りの後に SET_ZERO を送る (原点が今の姿勢へ書き換わる)",
    )
    parser.add_argument("--yes", action="store_true", help="--set-zero の確認を省略する")
    parser.add_argument(
        "--force", action="store_true", help="他プロセスがバスを使っていても続行する"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.samples < 1:
        raise SystemExit("--samples は 1 以上で指定してください")

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

    drivers, bus_alias = _build_drivers(spec, dict(robot.motors), args.axis)
    channel = system.can_buses.get(bus_alias)
    if channel is None:
        raise SystemExit(f"バス別名 '{bus_alias}' が {args.system} の can_buses にありません")

    _print_header(args, spec, group, drivers, channel, bus_alias, positions_path)

    bus = _open_bus(channel)
    try:
        talker = _detect_other_talker(bus, drivers)
        if talker is not None and not args.force:
            raise SystemExit(
                f"こちらが 1 通も送っていないのに {talker} のフィードバックが流れています。"
                " main.py などがバスを使っています。"
                "サーバーは起動時に SET_ZERO を送るので、"
                "同時に走らせるとこの確認は成立しません (承知の上なら --force)"
            )

        samples, params = _measure(bus, args, spec, group, drivers)

        if args.set_zero:
            if not _confirm_set_zero(drivers, args.yes):
                print("  中止しました (SET_ZERO は送っていません)")
            else:
                _send_set_zero(bus, drivers)
                print("  SET_ZERO を送りました。書き換わった零点を読み直します")
                print()
                samples, params = _measure(bus, args, spec, group, drivers)
    except ProbeError as exc:
        print(f"\n[ERR ] {exc}", file=sys.stderr)
        return 3
    finally:
        with contextlib.suppress(Exception):
            bus.shutdown()

    if args.json:
        payload = _as_json(args, spec, group, channel, samples, params)
        try:
            pathlib.Path(args.json).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            print(f"\n[WARN] JSON を書き出せません: {exc}", file=sys.stderr)
            return 1
        print(f"\n読んだ値を書き出しました: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
