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
#: 動作確認は両ハンド 1 本のシーケンス (sequences/motor_check.py) へ移り、合否は
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


class TestOriginCaptureCapability:
    """「`SET_ZERO` で原点を切り直せるか」はドライバ自身が宣言する。

    `main.py` や UI にドライバ種別を書き写して導出し直すと、ドライバを足した人が
    そちらの表を直し忘れる (`TelemetrySupport` と同じ方針)。
    """

    def test_既定は手段なし(self) -> None:
        driver = _ProtocolOnlyDriver("x", 1)
        assert driver.supports_origin_capture() is False
        assert driver.deactivation_steps() == []
        assert driver.origin_capture_steps() == []

    def test_edulite_は切り直せる(self) -> None:
        driver = Edulite05Driver("rotate_r", can_id=0x11)
        assert driver.supports_origin_capture() is True

    def test_edulite_は無励磁にしてから切り直す(self) -> None:
        """励磁したまま送るとドライバ内部の位置目標が旧座標のまま残り、軸が飛ぶ。"""
        driver = Edulite05Driver("rotate_r", can_id=0x11)

        ((disable, _delay),) = driver.deactivation_steps()
        ((set_zero, _zero_delay),) = driver.origin_capture_steps()

        assert Edulite05Driver.parse_can_id(disable.arbitration_id)[0] == (
            Edulite05Driver.COMM_TYPE_DISABLE
        )
        assert Edulite05Driver.parse_can_id(set_zero.arbitration_id)[0] == (
            Edulite05Driver.COMM_TYPE_SET_ZERO
        )

    def test_dm3520_は対象外(self) -> None:
        """`SET_ZERO` の安全な順序は disable を要求するが、`sub_lift` は disable
        すると自重で落ちる (減速比 19.2 のギヤに乗っているだけで保持ブレーキが無い)。
        """
        driver = Dm3520Driver("sub_lift_m", can_id=0x01, master_id=0x11)
        assert driver.supports_origin_capture() is False

    def test_generic_は対象外(self) -> None:
        assert GenericDriver("servo", can_id=0x41).supports_origin_capture() is False

    def test_m3508_は対象外(self) -> None:
        """累積角の原点は PC 側 (`M3508PositionLoop`) が持つ。CAN で送る原点は無い。"""
        assert M3508Driver("y_axis_r", can_id=1).supports_origin_capture() is False

    def test_切り直すフレームだけでは名乗れない(self) -> None:
        """無励磁にする手段が無いまま能力ありと名乗ると、**励磁したまま原点を
        動かす経路が黙って通る。**
        """

        class _HalfDeclared(_ProtocolOnlyDriver):
            def origin_capture_steps(self) -> list[tuple[can.Message, float]]:
                return [(can.Message(arbitration_id=1, data=bytes(8)), 0.0)]

        assert _HalfDeclared("x", 1).supports_origin_capture() is False


class TestFirmwareConfirmedCapability:
    """`INFO` (仕様書 §3.4) を送らないドライバは `firmware_confirmed()` が None のまま
    (`is_energized()` と同じ「申告そのものを持たない」の表現)。

    None を False へ倒すと、INFO を送らない 3 種の全モータが常時「未確認」として
    `RobotServer._firmware_unconfirmed_motors` に載ってしまう。
    """

    def test_既定は_None(self) -> None:
        assert _ProtocolOnlyDriver("x", 1).firmware_confirmed() is None

    def test_m3508_は対象外(self) -> None:
        assert M3508Driver("y_axis_r", can_id=1).firmware_confirmed() is None

    def test_edulite_は対象外(self) -> None:
        assert Edulite05Driver("rotate_r", can_id=0x11).firmware_confirmed() is None

    def test_dm3520_は対象外(self) -> None:
        driver = Dm3520Driver("sub_lift_m", can_id=0x01, master_id=0x11)
        assert driver.firmware_confirmed() is None

    def test_generic_は自己申告の有無を返す(self) -> None:
        """`GenericDriver` だけが INFO を送るので、ここだけ bool を返す。"""
        assert GenericDriver("gripper", 0x40).firmware_confirmed() is False
