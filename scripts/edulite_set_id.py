from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass

import can

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from lib.drivers.edulite05 import Edulite05Driver

_PROBE_WINDOW_S = 0.015

_PROBE_INTERVAL_S = 0.01


@dataclass(frozen=True)
class ScanResult:
    can_id: int
    responses: int


def probe_id(bus: can.BusABC, host_id: int, target: int) -> ScanResult:
    driver = Edulite05Driver(f"probe_{target}", can_id=target, host_id=host_id)
    bus.send(driver.feedback_probe_message())

    responses = 0
    deadline = time.monotonic() + _PROBE_WINDOW_S
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        msg = bus.recv(timeout=remaining)
        if msg is not None and driver.matches_feedback(msg):
            responses += 1
    return ScanResult(can_id=target, responses=responses)


def scan(bus: can.BusABC, host_id: int, targets: range) -> list[ScanResult]:
    found: list[ScanResult] = []
    stalled = 0
    for target in targets:
        try:
            result = probe_id(bus, host_id, target)
        except can.CanOperationError:
            stalled += 1
            time.sleep(0.05)
            continue
        if result.responses:
            found.append(result)
        time.sleep(_PROBE_INTERVAL_S)

    if stalled and not found:
        raise BusStalledError(stalled, len(targets))
    return found


class BusStalledError(RuntimeError):
    def __init__(self, stalled: int, total: int) -> None:
        super().__init__(
            f"{total} 通中 {stalled} 通の送信が詰まり、応答は 0 件でした。\n"
            "  **この結果だけでは原因を断定できません。** まずバスを張り直してください:\n"
            "      scripts/setup_can.sh\n"
            "  直前の走査で詰まったキューが残っているだけなら、これで直ります\n"
            "  (バスが正常でも同じ「応答 0 件」になるため、見分けが付きません)。\n"
            "\n"
            "  張り直しても応答 0 件なら、ACK を返すノードがバス上に居ません:\n"
            "    - モータに 24V が入っているか (**本機は CAN トランシーバも\n"
            "      モータ電源から取る**ので、無通電の個体はバス上に存在しない)\n"
            "    - CAN の H/L が入れ替わっていないか\n"
            "    - バス両端に 120Ω の終端が入っているか\n"
            "    - CANable と機体の GND が繋がっているか"
        )


def plan_set_id(source: int, target: int, scanned: list[ScanResult]) -> str | None:
    if source == target:
        return f"書き換え前後の ID が同じです (0x{source:02X})"

    source_hits = next((r for r in scanned if r.can_id == source), None)
    if source_hits is None:
        return f"ID 0x{source:02X} からの応答がありません。 scan で実際の ID を確かめてください"
    if source_hits.responses > 1:
        return (
            f"ID 0x{source:02X} に {source_hits.responses} 台がぶら下がっています。\n"
            "  1 通の書き換えフレームを全台が受け取り、**全台が同じ新 ID になります**。\n"
            "  書き換える 1 台だけを残して、他は電源を落としてから実行してください"
        )

    if any(r.can_id == target for r in scanned):
        return (
            f"ID 0x{target:02X} は既に使われています。"
            " 重複させると片方が永久にフィードバックを得られません"
        )
    return None


def _print_scan(found: list[ScanResult]) -> None:
    if not found:
        print("応答した EDULITE 05 はありません。")
        return
    print(f"応答した EDULITE 05: {len(found)} 個の ID")
    for result in found:
        note = "  <-- 同じ ID の個体が複数います" if result.responses > 1 else ""
        print(
            f"  can_id = 0x{result.can_id:02X} ({result.can_id:3d})"
            f"  応答 {result.responses} 通{note}"
        )


def cmd_scan(args: argparse.Namespace, bus: can.BusABC) -> int:
    found = scan(bus, args.host_id, range(args.scan_from, args.scan_to + 1))
    _print_scan(found)
    return 0


def cmd_set(args: argparse.Namespace, bus: can.BusABC) -> int:
    found = scan(bus, args.host_id, range(0x00, 0x100))
    _print_scan(found)

    reason = plan_set_id(args.source, args.target, found)
    if reason is not None:
        print(f"\n[拒否] {reason}", file=sys.stderr)
        return 1

    print(f"\n0x{args.source:02X} -> 0x{args.target:02X} へ書き換えます。")
    if not args.yes:
        answer = input("実行しますか? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("中止しました。")
            return 1

    driver = Edulite05Driver("target", can_id=args.source, host_id=args.host_id)
    bus.send(driver.encode_disable())
    time.sleep(0.1)
    bus.send(driver.encode_set_id(args.target))
    time.sleep(0.5)

    after_new = probe_id(bus, args.host_id, args.target)
    after_old = probe_id(bus, args.host_id, args.source)
    if after_new.responses == 1 and after_old.responses == 0:
        print(f"[ OK ] can_id = 0x{args.target:02X} で応答しました。")
        return 0

    print(
        f"[WARN] 書き換え後の照合に失敗しました "
        f"(新 ID の応答 {after_new.responses} 通 / 旧 ID の応答 {after_old.responses} 通)。\n"
        "  ファームによっては電源を入れ直すまで反映されません。"
        " 再投入してから scan で確かめてください。",
        file=sys.stderr,
    )
    return 2


def _auto_int(text: str) -> int:
    return int(text, 0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="EDULITE 05 の CAN ID を走査・書き換えする",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--bus", default="can_edulite", help="SocketCAN インタフェース名")
    parser.add_argument(
        "--host-id", type=_auto_int, default=0xFD, help="ホスト ID (config の host_id と揃える)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    scan_parser = sub.add_parser("scan", help="応答する CAN ID を走査する")
    scan_parser.add_argument("--scan-from", type=_auto_int, default=0x00)
    scan_parser.add_argument("--scan-to", type=_auto_int, default=0xFF)
    scan_parser.set_defaults(func=cmd_scan)

    set_parser = sub.add_parser("set", help="CAN ID を書き換える (1 台ずつ)")
    set_parser.add_argument(
        "--from", dest="source", type=_auto_int, required=True, help="現在の ID (出荷値は 0x7F)"
    )
    set_parser.add_argument("--to", dest="target", type=_auto_int, required=True, help="新しい ID")
    set_parser.add_argument("--yes", action="store_true", help="確認を省略する")
    set_parser.set_defaults(func=cmd_set)

    args = parser.parse_args(argv)

    bus = can.interface.Bus(channel=args.bus, interface="socketcan")
    try:
        return args.func(args, bus)
    except BusStalledError as exc:
        print(f"[ERR ] {exc}", file=sys.stderr)
        return 3
    finally:
        bus.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
