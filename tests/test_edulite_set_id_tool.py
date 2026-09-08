from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

_SCRIPT_PATH = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "edulite_set_id.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("edulite_set_id", _SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # @dataclass は型注釈の解決でモジュールを引くので、exec_module の前に登録する。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


edulite_set_id = _load_module()

ScanResult = edulite_set_id.ScanResult
plan_set_id = edulite_set_id.plan_set_id


def test_allows_a_single_motor_moving_to_a_free_id() -> None:
    assert plan_set_id(0x7F, 0x01, [ScanResult(0x7F, 1)]) is None


def test_refuses_when_several_motors_share_the_source_id() -> None:
    reason = plan_set_id(0x7F, 0x01, [ScanResult(0x7F, 2)])

    assert reason is not None
    assert "2 台" in reason


def test_refuses_when_the_destination_id_is_already_taken() -> None:
    reason = plan_set_id(0x7F, 0x01, [ScanResult(0x7F, 1), ScanResult(0x01, 1)])

    assert reason is not None
    assert "既に使われています" in reason


def test_refuses_when_the_source_id_never_answered() -> None:
    reason = plan_set_id(0x7F, 0x01, [ScanResult(0x02, 1)])

    assert reason is not None
    assert "応答がありません" in reason


def test_refuses_a_no_op_rewrite() -> None:
    assert plan_set_id(0x01, 0x01, [ScanResult(0x01, 1)]) is not None


@pytest.mark.parametrize("responses", [2, 3, 7])
def test_any_number_of_duplicates_is_refused(responses: int) -> None:
    assert plan_set_id(0x7F, 0x01, [ScanResult(0x7F, responses)]) is not None


def test_stalled_bus_message_offers_the_recoverable_cause_first() -> None:
    message = str(edulite_set_id.BusStalledError(8, 256))

    assert "setup_can.sh" in message, "張り直しの手順を出していない"
    assert "断定できません" in message, "原因を断定しない旨が消えている"
    assert message.index("setup_can.sh") < message.index("24V")
