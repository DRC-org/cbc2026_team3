"""scripts/edulite_set_id.py の書き換え可否判定。

**ID 書き換えは取り消しの効かない操作**なので、拒否条件だけを単独で確かめる。
実バスは要らない —— 判定は走査結果 (ScanResult のリスト) の純関数として
切り出してあり、CAN を立てずに全条件を通せる。
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

_SCRIPT_PATH = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "edulite_set_id.py"


def _load_module():
    """scripts/ はパッケージではないのでファイルパスから直接読み込む。

    tests/test_can_config.py と同じ理由 —— スタンドアロンスクリプトとして起動する
    形態を保ったままテストしたいので、パッケージ化して import 経路を作らない。

    **exec_module の前に sys.modules へ入れる必要がある。** @dataclass は
    `sys.modules[cls.__module__]` を引いて型注釈を解決するため、登録前に実行すると
    AttributeError で読み込みそのものが落ちる。
    """
    spec = importlib.util.spec_from_file_location("edulite_set_id", _SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


edulite_set_id = _load_module()

ScanResult = edulite_set_id.ScanResult
plan_set_id = edulite_set_id.plan_set_id


def test_allows_a_single_motor_moving_to_a_free_id() -> None:
    assert plan_set_id(0x7F, 0x01, [ScanResult(0x7F, 1)]) is None


def test_refuses_when_several_motors_share_the_source_id() -> None:
    """出荷直後の構成そのもの。**1 通で全台が同じ新 ID になる**ので必ず止める。"""
    reason = plan_set_id(0x7F, 0x01, [ScanResult(0x7F, 2)])

    assert reason is not None
    assert "2 台" in reason


def test_refuses_when_the_destination_id_is_already_taken() -> None:
    """重複させると片方が永久にフィードバックを得られない。"""
    reason = plan_set_id(0x7F, 0x01, [ScanResult(0x7F, 1), ScanResult(0x01, 1)])

    assert reason is not None
    assert "既に使われています" in reason


def test_refuses_when_the_source_id_never_answered() -> None:
    """応答の無い ID へ投げても届かない。「書けたつもり」で終わるのを防ぐ。"""
    reason = plan_set_id(0x7F, 0x01, [ScanResult(0x02, 1)])

    assert reason is not None
    assert "応答がありません" in reason


def test_refuses_a_no_op_rewrite() -> None:
    assert plan_set_id(0x01, 0x01, [ScanResult(0x01, 1)]) is not None


@pytest.mark.parametrize("responses", [2, 3, 7])
def test_any_number_of_duplicates_is_refused(responses: int) -> None:
    assert plan_set_id(0x7F, 0x01, [ScanResult(0x7F, responses)]) is not None


def test_stalled_bus_message_offers_the_recoverable_cause_first() -> None:
    """「応答 0 件」から物理層の断線を**断定してはならない**。

    直前の走査で詰まった送信キューが残っているだけでも、まったく同じ
    「送信が詰まる + 応答 0 件」になる。実際にこれで「モータが繋がっていない」と
    誤診した (2 台とも生きていた)。復旧できる側の原因とその手順を先に出すこと。
    """
    message = str(edulite_set_id.BusStalledError(8, 256))

    assert "setup_can.sh" in message, "張り直しの手順を出していない"
    assert "断定できません" in message, "原因を断定しない旨が消えている"
    # 張り直しの案内が、物理層の疑いより先に来ること
    assert message.index("setup_can.sh") < message.index("24V")
