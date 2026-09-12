#!/usr/bin/env python3
from __future__ import annotations

import argparse
import pathlib
import sys

import yaml

_PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
_CONFIG_PATH = _PROJECT_ROOT / "config" / "can_buses.yaml"

UNASSIGNED = "TBD"

_IFNAME_MAX = 15

_SERVICE_NAME = "cbc-can.service"

# slcand の常駐はバス 1 本につき 1 インスタンス。起こす責任はこの unit だけが持つ。
_SLCAND_SERVICE_TEMPLATE = "cbc-slcand@.service"

_UDEV_RULE_PATH = "/etc/udev/rules.d/99-canable.rules"

# 100ms は bus-off 自動復帰周期のカーネル推奨値。
DEFAULT_RESTART_MS = 100

LINK_GS_USB = "gs_usb"
LINK_SLCAN = "slcan"
_LINKS = (LINK_GS_USB, LINK_SLCAN)

# slcand -s<code>。can-utils の slcand(8) が持つ標準ビットレート表がそのまま上限。
_SLCAN_SPEED_CODES = {
    10000: "0",
    20000: "1",
    50000: "2",
    100000: "3",
    125000: "4",
    250000: "5",
    500000: "6",
    800000: "7",
    1000000: "8",
}

_USB_ID_KEYS = ("vendor_id", "product_id")
_USB_ID_ATTRS = {"vendor_id": "idVendor", "product_id": "idProduct"}


class ConfigError(Exception):
    """can_buses.yaml の内容が不正な場合に送出する。"""


def load_config(path: pathlib.Path) -> dict:
    if not path.exists():
        raise ConfigError(f"設定ファイルが見つかりません: {path}")
    with open(path) as f:
        config = yaml.safe_load(f) or {}

    buses = config.get("buses")
    if not buses:
        raise ConfigError(f"'buses' セクションが空です: {path}")

    usb = config.get("usb") or {}
    for key in _USB_ID_KEYS:
        if not usb.get(key):
            raise ConfigError(f"'usb.{key}' が未設定です: {path}")

    for name, entry in buses.items():
        entry = entry or {}
        if len(name) > _IFNAME_MAX:
            raise ConfigError(
                f"バス名 '{name}' が長すぎます ({len(name)} 文字, 上限 {_IFNAME_MAX})"
            )
        bitrate = entry.get("bitrate")
        if not bitrate:
            raise ConfigError(f"バス '{name}' の bitrate が未設定です")

        link = _link(entry)
        if link not in _LINKS:
            raise ConfigError(
                f"バス '{name}' の link が不正です: {link!r} (使えるのは {', '.join(_LINKS)})"
            )
        if link != LINK_SLCAN:
            continue

        if int(bitrate) not in _SLCAN_SPEED_CODES:
            supported = ", ".join(str(v) for v in sorted(_SLCAN_SPEED_CODES))
            raise ConfigError(
                f"バス '{name}' の bitrate {bitrate} は slcand が扱えません"
                f" (使えるのは {supported})"
            )
        for key in _USB_ID_KEYS:
            if not entry.get(key):
                raise ConfigError(
                    f"バス '{name}' は link: {LINK_SLCAN} なので {key} をバス側に持つ必要があります"
                )

    return config


def _link(entry: dict) -> str:
    return str((entry or {}).get("link", LINK_GS_USB)).strip()


def _value(entry: dict, key: str) -> str:
    return str(entry.get(key, "")).strip()


def _is_set(entry: dict, key: str) -> bool:
    value = _value(entry, key)
    return bool(value) and value != UNASSIGNED


def slcan_device(name: str) -> str:
    """slcand へ渡す tty の固定パス。udev の SYMLINK+= と対にする。"""
    return f"/dev/{name}_tty"


def slcand_unit(name: str) -> str:
    """そのバスの slcand を持つ systemd インスタンス名。"""
    return _SLCAND_SERVICE_TEMPLATE.replace("@.", f"@{name}.")


def _is_assigned(entry: dict) -> bool:
    if not _is_set(entry, "serial"):
        return False
    # slcan は tty を個体固定するので VID/PID も揃わないとルールを書けない。
    if _link(entry) == LINK_SLCAN:
        return all(_is_set(entry, key) for key in _USB_ID_KEYS)
    return True


def cmd_list(config: dict, *, assigned_only: bool) -> str:
    lines = []
    for name, entry in config["buses"].items():
        entry = entry or {}
        if assigned_only and not _is_assigned(entry):
            continue
        serial = str(entry.get("serial", UNASSIGNED)).strip() or UNASSIGNED
        bitrate = int(entry["bitrate"])
        txqueuelen = int(entry.get("txqueuelen", 1000))
        restart_ms = int(entry.get("restart_ms", DEFAULT_RESTART_MS))
        lines.append(f"{name}\t{serial}\t{bitrate}\t{txqueuelen}\t{restart_ms}")
    return "\n".join(lines)


def cmd_slcan(config: dict, *, assigned_only: bool = False) -> str:
    """slcand で上げるバスを TSV で返す: 名前 / tty パス / -s のコード / unit 名。

    既定で未採取のバスも出す —— 利用側が「このバスは slcand で上げる」を知る単一情報源。
    """
    lines = []
    for name, entry in config["buses"].items():
        entry = entry or {}
        if _link(entry) != LINK_SLCAN:
            continue
        if assigned_only and not _is_assigned(entry):
            continue
        speed = _SLCAN_SPEED_CODES[int(entry["bitrate"])]
        lines.append(f"{name}\t{slcan_device(name)}\t{speed}\t{slcand_unit(name)}")
    return "\n".join(lines)


def cmd_udev(config: dict) -> str:
    usb = config["usb"]
    vendor = usb["vendor_id"]
    product = usb["product_id"]
    match = (
        'SUBSYSTEM=="net", ACTION=="add", '
        f'ATTRS{{idVendor}}=="{vendor}", ATTRS{{idProduct}}=="{product}"'
    )
    restart = f'RUN+="/usr/bin/systemctl --no-block restart {_SERVICE_NAME}"'

    lines = [
        "# 自動生成ファイル — 直接編集しないこと。",
        "# 生成元: config/can_buses.yaml / scripts/can_config.py",
        "# 再生成: sudo scripts/install.sh",
        "",
    ]

    slcan_lines = []
    for name, entry in config["buses"].items():
        entry = entry or {}
        if _link(entry) == LINK_SLCAN:
            slcan_lines += _slcan_rules(name, entry, restart)
            continue
        if not _is_assigned(entry):
            lines.append(f"# {name}: serial 未採取のためルール未生成")
            continue
        serial = str(entry["serial"]).strip()
        lines.append(f'{match}, ATTRS{{serial}}=="{serial}", NAME="{name}"')

    lines += [
        "",
        "# 抜き差し時にバス設定をやり直す。setup_can.sh は冪等なので、",
        "# 複数個体が同時に認識されて多重起動しても問題ない。",
        "# --no-block は udev のイベント処理をブロックしないために必須。",
        f"{match}, {restart}",
        "",
    ]

    if slcan_lines:
        lines += [
            "# CAN トランシーバを持たない基板 (USB CDC + slcand)。",
            "# netdev は slcand が作るので、ここでは tty を個体固定するだけ。",
            *slcan_lines,
            "",
        ]
    return "\n".join(lines)


def _slcan_rules(name: str, entry: dict, restart: str) -> list[str]:
    if not _is_assigned(entry):
        missing = [key for key in ("serial", *_USB_ID_KEYS) if not _is_set(entry, key)]
        return [f"# {name}: {' / '.join(missing)} 未採取のためルール未生成"]

    ids = ", ".join(
        f'ATTRS{{{_USB_ID_ATTRS[key]}}}=="{_value(entry, key)}"' for key in _USB_ID_KEYS
    )
    match = f'SUBSYSTEM=="tty", {ids}, ATTRS{{serial}}=="{_value(entry, "serial")}"'
    symlink = slcan_device(name).removeprefix("/dev/")
    return [
        # ACTION を絞らないのは、change イベントで udev が symlink を消さないようにするため。
        f'{match}, SYMLINK+="{symlink}"',
        f'{match}, ACTION=="add", {restart}',
    ]


def cmd_paths() -> str:
    return "\n".join(
        [
            f"udev_rule_path\t{_UDEV_RULE_PATH}",
            f"service_name\t{_SERVICE_NAME}",
            f"slcand_service_template\t{_SLCAND_SERVICE_TEMPLATE}",
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="CAN バス定義の変換ツール")
    parser.add_argument(
        "command",
        choices=["list", "udev", "paths", "slcan"],
        help=(
            "list: TSV 出力 / udev: udev ルール生成 / paths: 固定パスの TSV 出力"
            " / slcan: slcand で上げるバスの TSV 出力"
        ),
    )
    parser.add_argument(
        "--assigned-only",
        action="store_true",
        help="serial 採取済みのバスのみ出力 (list / slcan のみ有効)",
    )
    parser.add_argument("--config", type=pathlib.Path, default=_CONFIG_PATH)
    args = parser.parse_args()

    if args.command == "paths":
        print(cmd_paths())
        return 0

    try:
        config = load_config(args.config)
    except (ConfigError, yaml.YAMLError) as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1

    if args.command == "list":
        output = cmd_list(config, assigned_only=args.assigned_only)
    elif args.command == "slcan":
        output = cmd_slcan(config, assigned_only=args.assigned_only)
    else:
        output = cmd_udev(config)

    if output:
        print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
