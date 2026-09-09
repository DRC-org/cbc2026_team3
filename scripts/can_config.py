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

_UDEV_RULE_PATH = "/etc/udev/rules.d/99-canable.rules"

# 100ms は bus-off 自動復帰周期のカーネル推奨値。
DEFAULT_RESTART_MS = 100


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
    for key in ("vendor_id", "product_id"):
        if not usb.get(key):
            raise ConfigError(f"'usb.{key}' が未設定です: {path}")

    for name, entry in buses.items():
        if len(name) > _IFNAME_MAX:
            raise ConfigError(
                f"バス名 '{name}' が長すぎます ({len(name)} 文字, 上限 {_IFNAME_MAX})"
            )
        if not (entry or {}).get("bitrate"):
            raise ConfigError(f"バス '{name}' の bitrate が未設定です")

    return config


def _is_assigned(entry: dict) -> bool:
    serial = str(entry.get("serial", "")).strip()
    return bool(serial) and serial != UNASSIGNED


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


def cmd_udev(config: dict) -> str:
    usb = config["usb"]
    vendor = usb["vendor_id"]
    product = usb["product_id"]
    match = (
        'SUBSYSTEM=="net", ACTION=="add", '
        f'ATTRS{{idVendor}}=="{vendor}", ATTRS{{idProduct}}=="{product}"'
    )

    lines = [
        "# 自動生成ファイル — 直接編集しないこと。",
        "# 生成元: config/can_buses.yaml / scripts/can_config.py",
        "# 再生成: sudo scripts/install.sh",
        "",
    ]

    for name, entry in config["buses"].items():
        entry = entry or {}
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
        f'{match}, RUN+="/usr/bin/systemctl --no-block restart {_SERVICE_NAME}"',
        "",
    ]
    return "\n".join(lines)


def cmd_paths() -> str:
    return "\n".join(
        [
            f"udev_rule_path\t{_UDEV_RULE_PATH}",
            f"service_name\t{_SERVICE_NAME}",
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="CAN バス定義の変換ツール")
    parser.add_argument(
        "command",
        choices=["list", "udev", "paths"],
        help="list: TSV 出力 / udev: udev ルール生成 / paths: 固定パスの TSV 出力",
    )
    parser.add_argument(
        "--assigned-only",
        action="store_true",
        help="serial 採取済みのバスのみ出力 (list のみ有効)",
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
    else:
        output = cmd_udev(config)

    if output:
        print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
