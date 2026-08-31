"""MotorDriver の契約に対するテスト。

個々のプロトコル実装ではなく「全ドライバが守るべき約束」を検証する。
"""

from __future__ import annotations

import can
import pytest

from lib.drivers.base import ControlMode, MotorDriver, MotorState
from lib.drivers.dm3520 import Dm3520Driver
from lib.drivers.edulite05 import Edulite05Driver
from lib.drivers.generic import GenericDriver
from lib.drivers.m3508 import M3508Driver

#: モータ単位の能動テスト (旧 MotorCheckRunner) 用に置かれていた語彙。
#: 動作確認は両ハンド 1 本のシーケンス (robots/motor_check.py) へ移り、合否は
#: シーケンスエンジンの到達判定が担うので、この API 群は 1 つも復活させない
_REMOVED_CHECK_API = (
    "check_command",
    "evaluate_check_result",
    "reset_after_check",
    "check_tolerance",
    "evaluate_tracking",
    "prepare_check_steps",
    "check_safety_error",
    "requires_fresh_feedback_for_check",
)

_ALL_DRIVERS = (MotorDriver, GenericDriver, M3508Driver, Edulite05Driver, Dm3520Driver)


class _ProtocolOnlyDriver(MotorDriver):
    """プロトコル層だけを実装したドライバ。"""

    def encode_target(self, mode: ControlMode, value: float) -> can.Message:
        return can.Message(arbitration_id=1, data=bytes(8))

    def decode_feedback(self, msg: can.Message) -> MotorState:
        return MotorState()

    def matches_feedback(self, msg: can.Message) -> bool:
        return False


class TestDeadApisAreRemoved:
    """「テストや診断だけが唯一の利用者」の API は残さない。"""

    def test_prepare_check_is_gone(self) -> None:
        assert not hasattr(MotorDriver, "prepare_check")
        assert not hasattr(Edulite05Driver, "prepare_check")

    def test_initialization_messages_is_gone(self) -> None:
        assert not hasattr(Edulite05Driver, "initialization_messages")

    def test_check_context_is_gone(self) -> None:
        import lib.drivers.base as base

        assert not hasattr(base, "CheckContext")

    @pytest.mark.parametrize("method", _REMOVED_CHECK_API)
    @pytest.mark.parametrize("driver_cls", _ALL_DRIVERS)
    def test_motor_check_api_is_gone(self, driver_cls: type, method: str) -> None:
        assert not hasattr(driver_cls, method)


class TestAbstractSurfaceIsProtocolOnly:
    """新しいドライバに書かせるのはプロトコル層の 3 つだけにする。

    呼ばれない実装を abstractmethod で強いると、書く側は「何のために要るのか」を
    確かめられないまま形だけ埋めることになる。
    """

    def test_abstract_methods_are_protocol_only(self) -> None:
        assert MotorDriver.__abstractmethods__ == frozenset(
            {"encode_target", "decode_feedback", "matches_feedback"}
        )

    def test_protocol_only_driver_can_be_instantiated(self) -> None:
        driver = _ProtocolOnlyDriver("x", 1)
        assert driver.name == "x"
