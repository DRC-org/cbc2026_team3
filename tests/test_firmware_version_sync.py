"""ファームの `kFirmwareVersion` と config の `expected_firmware` の一致を機械的に守る。

**この 2 つがずれると「正しく焼いたのに全モータ FAULT」になる。** PC 側は INFO
(1Hz の自己申告, 仕様書 §3.4) を `expected_firmware` と突き合わせ、食い違ったら
そのモータを FAULT にする —— 焼き忘れを見つけるための仕掛けなので、ファームだけを
上げて config を据え置くと、正しく焼いた基板が一斉に FAULT になる。

規則自体は `firmware/README.md` と `config/*.yaml` のコメントに書いてあったが、
守るのは人の注意力だけだった。実際に servo=3 / dc=2 / solenoid=2 へ上げたときに
config 側 26 箇所が丸ごと取り残された。ここで突き合わせておけば、片方だけを
変えたコミットは `uv run pytest` で必ず落ちる。

対応付けはデバイス ID の上位 2bit (仕様書 §2.2 の固定ビット分割) で行う。
`config/*.yaml` に基板種別を書く欄は無く、can_id が唯一の手掛かりであるため。
"""

from __future__ import annotations

import pathlib
import re

import pytest
import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_CONFIG_DIR = _REPO_ROOT / "config"
_FIRMWARE_DIR = _REPO_ROOT / "firmware"

# 仕様書 §2.2: Bit7..6 が基板種別 (0=予約 / 1=サーボ / 2=DC / 3=電磁弁)。
_BOARD_KIND_TO_PROJECT = {
    1: "servo",
    2: "dc_motor",
    3: "solenoid",
}

_VERSION_RE = re.compile(r"constexpr\s+uint8_t\s+kFirmwareVersion\s*=\s*(\d+)\s*;")


def _firmware_version(project: str) -> int:
    """`firmware/<project>/include/config.h` が申告する版番号。"""
    header = _FIRMWARE_DIR / project / "include" / "config.h"
    matches = _VERSION_RE.findall(header.read_text(encoding="utf-8"))
    # 定義が消えた・複数になったら曖昧なので突き合わせを続けない
    # (0 件を「版番号なし」として素通しすると、この検査ごと黙って死ぬ)
    assert len(matches) == 1, f"{header}: kFirmwareVersion の定義が {len(matches)} 個"
    return int(matches[0])


def _generic_motors() -> list[tuple[pathlib.Path, str, dict]]:
    """同梱の全 yaml (bench の 6 セットを含む) から generic のモータ定義を拾う。"""
    found: list[tuple[pathlib.Path, str, dict]] = []
    for path in sorted(_CONFIG_DIR.rglob("*.yaml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(doc, dict):
            continue
        motors = doc.get("motors")
        if not isinstance(motors, dict):
            continue
        for name, motor in motors.items():
            if isinstance(motor, dict) and motor.get("driver") == "generic":
                found.append((path, name, motor))
    return found


_GENERIC_MOTORS = _generic_motors()


def _case_id(entry: tuple[pathlib.Path, str, dict]) -> str:
    path, name, _ = entry
    return f"{path.relative_to(_REPO_ROOT)}::{name}"


class TestFirmwareVersionSync:
    def test_shipped_configs_have_generic_motors(self):
        # 収集が空振りしたまま緑になるのを防ぐ (glob の書き間違い・config 移動)
        assert len(_GENERIC_MOTORS) >= 20

    @pytest.mark.parametrize("entry", _GENERIC_MOTORS, ids=_case_id)
    def test_expected_firmware_is_declared(self, entry):
        """generic のモータは必ず期待値を書く。

        書かない自由を残すと、下の突き合わせが「対象 0 件」で緑になる形で
        すり抜けられる。
        """
        path, name, motor = entry
        assert "expected_firmware" in motor, f"{path}: {name} に expected_firmware が無い"

    @pytest.mark.parametrize("entry", _GENERIC_MOTORS, ids=_case_id)
    def test_expected_firmware_matches_header(self, entry):
        path, name, motor = entry
        can_id = motor["can_id"]
        project = _BOARD_KIND_TO_PROJECT[(can_id >> 6) & 0b11]
        expected = _firmware_version(project)
        assert motor["expected_firmware"] == expected, (
            f"{path}: {name} (can_id=0x{can_id:02X}) の expected_firmware が "
            f"firmware/{project}/include/config.h の kFirmwareVersion={expected} と "
            f"食い違っている。片方だけ上げると、正しく焼いた基板が一斉に FAULT になる"
        )
