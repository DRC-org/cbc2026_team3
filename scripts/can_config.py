#!/usr/bin/env python3
"""config/can_buses.yaml を setup_can.sh と udev ルール向けの形式に変換する。

systemd から root で起動されるため、プロジェクトの .venv ではなくシステムの
python3 + pyyaml で動作することを前提にする。venv のパスに依存すると
起動順やユーザ切り替えで壊れるため。
"""

from __future__ import annotations

import argparse
import pathlib
import sys

import yaml

_PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
_CONFIG_PATH = _PROJECT_ROOT / "config" / "can_buses.yaml"

# serial 未採取を示すプレースホルダ。この値のバスは udev ルールにも
# setup_can.sh の対象にも含めない。
UNASSIGNED = "TBD"

# Linux の IFNAMSIZ は 16。終端 NUL を含むためインターフェース名は 15 文字まで。
_IFNAME_MAX = 15

_SERVICE_NAME = "cbc-can.service"

# bus-off からの自動復帰までの待ち時間 [ms]。**0 (カーネル既定) にしてはならない。**
# 0 は「自動復帰しない」の意味で、一度 bus-off に落ちたインタフェースは
# 手動で down/up するまで送受信とも死んだままになる。専用バスに 1 台しか
# 居ない構成 (can_dm3520) では相手が電源を失うだけで ACK が返らなくなり、
# TEC が 256 に達して bus-off へ落ちる。試合中にそれが起きると、
# 相手の電源が戻っても機体は二度と動かない。
# 100ms はカーネルの推奨値で、復帰を試みる周期でもある。
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
    """setup_can.sh が while read で回せる TSV を返す。"""
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
    """シリアル一致で固定名を割り当てる udev ルールを生成する。"""
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


def main() -> int:
    parser = argparse.ArgumentParser(description="CAN バス定義の変換ツール")
    parser.add_argument(
        "command",
        choices=["list", "udev"],
        help="list: TSV 出力 / udev: udev ルール生成",
    )
    parser.add_argument(
        "--assigned-only",
        action="store_true",
        help="serial 採取済みのバスのみ出力 (list のみ有効)",
    )
    parser.add_argument("--config", type=pathlib.Path, default=_CONFIG_PATH)
    args = parser.parse_args()

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
