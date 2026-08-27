"""MotorDriver の契約 (動作確認 API) に対するテスト。

個々のプロトコル実装ではなく「全ドライバが守るべき約束」を検証する。
"""

from __future__ import annotations

import inspect
import math

import can
import pytest

from lib.drivers.base import CheckContext, ControlMode, MotorDriver, MotorState
from lib.drivers.edulite05 import Edulite05Driver
from lib.drivers.generic import GenericDriver
from lib.drivers.m3508 import M3508Driver
from tests.feedback_frames import edulite_feedback, feed_generic


class _ProtocolOnlyDriver(MotorDriver):
    """プロトコル層だけを実装し、動作確認 API を持たないドライバ。"""

    def encode_target(self, mode: ControlMode, value: float) -> can.Message:
        return can.Message(arbitration_id=1, data=bytes(8))

    def decode_feedback(self, msg: can.Message) -> MotorState:
        return MotorState()

    def matches_feedback(self, msg: can.Message) -> bool:
        return False


class TestCheckApiIsMandatory:
    """動作確認 API は「実装しなくても起動する」状態にしてはならない。

    未実装のまま動くと、reset_after_check が送られないまま次のモータへ進む
    (駆動状態が残る) 事故が実行時まで表面化しない。
    """

    def test_driver_without_check_api_cannot_be_instantiated(self) -> None:
        with pytest.raises(TypeError):
            _ProtocolOnlyDriver("x", 1)

    @pytest.mark.parametrize(
        "method",
        ["check_command", "evaluate_check_result", "reset_after_check"],
    )
    def test_check_api_is_abstract(self, method: str) -> None:
        assert method in MotorDriver.__abstractmethods__


class TestDeadApisAreRemoved:
    """「テストや診断だけが唯一の利用者」の API は残さない。"""

    def test_prepare_check_is_gone(self) -> None:
        assert not hasattr(MotorDriver, "prepare_check")
        assert not hasattr(Edulite05Driver, "prepare_check")

    def test_initialization_messages_is_gone(self) -> None:
        assert not hasattr(Edulite05Driver, "initialization_messages")

    def test_evaluate_check_result_takes_only_context(self) -> None:
        # state は呼び出し元が常に motor.state を渡すだけの冗長引数、
        # tolerance は M3508 が無視していた「誰も渡さない引数」
        params = list(inspect.signature(MotorDriver.evaluate_check_result).parameters)
        assert params == ["self", "context"]


class TestCheckContextIsTyped:
    """context は文字列キーの手渡し辞書ではなく型付きにする。"""

    @pytest.mark.parametrize(
        ("driver", "magnitude"),
        [
            (GenericDriver("g", 0x01), 5.0),
            (Edulite05Driver("e", 5), 5.0),
            (M3508Driver("m", 1), 500.0),
        ],
    )
    def test_check_command_returns_check_context(
        self, driver: MotorDriver, magnitude: float
    ) -> None:
        _msg, context = driver.check_command(magnitude=magnitude)
        assert isinstance(context, CheckContext)
        assert isinstance(context.mode, ControlMode)


class TestToleranceHasSingleSource:
    """動作確認の許容差は default_tolerance だけが決める。"""

    def test_generic_check_follows_default_tolerance(self, monkeypatch) -> None:
        drv = GenericDriver("g", 0x01)
        _msg, context = drv.check_command(magnitude=10.0)
        feed_generic(drv, position=7.0, reached=True)

        assert drv.evaluate_check_result(context)[0] is False

        monkeypatch.setattr(GenericDriver, "default_tolerance", lambda self, mode: 4.0)
        assert drv.evaluate_check_result(context)[0] is True

    def test_edulite_check_follows_default_tolerance(self, monkeypatch) -> None:
        drv = Edulite05Driver("e", 5)
        drv.update_state(edulite_feedback(drv, position=0.0))
        _msg, context = drv.check_command(magnitude=10.0)
        drv.update_state(edulite_feedback(drv, position=math.radians(7.0)))

        assert drv.evaluate_check_result(context)[0] is False

        monkeypatch.setattr(
            Edulite05Driver, "default_tolerance", lambda self, mode: math.radians(4.0)
        )
        assert drv.evaluate_check_result(context)[0] is True


class TestStandstillNeverPasses:
    """指令量が許容差以下だと「動いていないモータ」が合格する。

    config の motor_check.default_magnitude.edulite05 は 5.0 で、velocity モードでは
    5rpm と解釈される。既定許容差も 5rpm なので |0 - 5rpm| <= 5rpm が成立してしまう。
    """

    def test_edulite_velocity_standstill_fails(self) -> None:
        drv = Edulite05Driver("e", 5, mode="velocity", limit_speed=2.0)
        drv.update_state(edulite_feedback(drv, velocity=0.0))
        _msg, context = drv.check_command(magnitude=5.0)

        passed, detail = drv.evaluate_check_result(context)

        assert passed is False
        assert detail is not None

    def test_edulite_velocity_following_command_passes(self) -> None:
        drv = Edulite05Driver("e", 5, mode="velocity", limit_speed=2.0)
        drv.update_state(edulite_feedback(drv, velocity=0.0))
        _msg, context = drv.check_command(magnitude=5.0)
        drv.update_state(edulite_feedback(drv, velocity=context.target))

        assert drv.evaluate_check_result(context)[0] is True

    def test_generic_velocity_standstill_fails(self) -> None:
        drv = GenericDriver("g", 0x01, control_type=ControlMode.VELOCITY)
        _msg, context = drv.check_command(magnitude=5.0)
        feed_generic(drv)

        passed, detail = drv.evaluate_check_result(context)

        assert passed is False
        assert detail is not None

    def test_generic_position_standstill_fails(self) -> None:
        # 位置モードでも同じ構造の穴がある。指令変位 0.5deg は許容差 1deg 未満なので
        # 一歩も動かなくても「目標との差 0.5deg」で合格してしまう
        drv = GenericDriver("g", 0x01)
        _msg, context = drv.check_command(magnitude=0.5)
        feed_generic(drv, position=0.0, reached=True)

        assert drv.evaluate_check_result(context)[0] is False
