from __future__ import annotations

import asyncio
import struct

import can
import pytest

from lib.control.target_refresh import (
    DEFAULT_INTERVAL_S,
    FIRMWARE_COMMAND_TIMEOUT_S,
    GenericTargetRefresher,
    QueryDrivenTargetRefresher,
)
from lib.drivers.base import ControlMode
from lib.drivers.dm3520 import Dm3520Driver
from lib.drivers.generic import GenericDriver
from lib.sequence.motors import MotorHandle
from tests.feedback_frames import feed_dm3520


class _StubCANManager:
    """MotorHandle が触る API だけを実装したスタブ。

    tests/test_position_loop.py の同名スタブとは意図的に別物 (あちらは
    ``send_to_bus`` だけを持つ)。触れる API を協力者ごとに絞ることで、
    本来使ってはならない経路をテストが使い始めても気付けるようにしている。
    """

    def __init__(self) -> None:
        self.sent: list[tuple[str, can.Message]] = []
        self.fail_motors: set[str] = set()

    async def send(self, motor_name: str, msg: can.Message) -> None:
        if motor_name in self.fail_motors:
            raise can.CanError("送信失敗 (テスト)")
        self.sent.append((motor_name, msg))

    def names(self) -> list[str]:
        return [name for name, _ in self.sent]


def _target_value(msg: can.Message) -> float:
    # SET_TARGET は Byte1-2 の int16 固定小数点 (仕様書 §3.1 / §4)
    return struct.unpack_from("<h", msg.data, 1)[0] / 10000.0


class _Fixture:
    """再送タスク + generic モータ 2 台。"""

    def __init__(self) -> None:
        self.manager = _StubCANManager()
        self.estop = False
        self.conveyor = GenericDriver("conveyor", can_id=1, control_type=ControlMode.DUTY)
        self.wall = GenericDriver("wall_f", can_id=2)
        self.handles = {
            driver.name: MotorHandle(
                driver.name,
                driver,
                self.manager,  # type: ignore[arg-type]
                is_estop_active=lambda: self.estop,
            )
            for driver in (self.conveyor, self.wall)
        }
        self.refresher = GenericTargetRefresher(
            list(self.handles.values()),
            is_estop_active=lambda: self.estop,
        )

    def clear_sent(self) -> None:
        self.manager.sent.clear()


class TestResend:
    async def test_resends_last_target(self) -> None:
        """ファームは 500ms 指令が来ないと出力を止める。再送が無いとコンベアが止まる。"""
        fx = _Fixture()
        await fx.handles["conveyor"].set_target(ControlMode.DUTY, 0.3)
        fx.clear_sent()

        await fx.refresher.step()

        assert fx.manager.names() == ["conveyor"]
        assert _target_value(fx.manager.sent[0][1]) == pytest.approx(0.3)

    async def test_resends_every_step(self) -> None:
        fx = _Fixture()
        await fx.handles["conveyor"].set_target(ControlMode.DUTY, 0.3)
        fx.clear_sent()

        await fx.refresher.step()
        await fx.refresher.step()
        await fx.refresher.step()

        assert fx.manager.names() == ["conveyor"] * 3

    async def test_no_send_without_target(self) -> None:
        """起動直後に勝手な指令を出さない (意図しない駆動を作らない)。"""
        fx = _Fixture()

        await fx.refresher.step()

        assert fx.manager.sent == []

    async def test_target_is_not_altered_by_resend(self) -> None:
        fx = _Fixture()
        await fx.handles["conveyor"].set_target(ControlMode.DUTY, 0.3)

        await fx.refresher.step()

        assert fx.handles["conveyor"].target == pytest.approx(0.3)
        assert fx.handles["conveyor"].mode is ControlMode.DUTY


class TestEStopInterlock:
    async def test_no_send_while_estop_active(self) -> None:
        """再送は停止指令を上書きする。緊急停止中に出してはならない。"""
        fx = _Fixture()
        await fx.handles["conveyor"].set_target(ControlMode.DUTY, 0.3)
        fx.clear_sent()
        fx.estop = True

        await fx.refresher.step()

        assert fx.manager.sent == []

    async def test_clear_targets_prevents_restart_after_release(self) -> None:
        """緊急停止解除だけでコンベアが回り出してはならない。"""
        fx = _Fixture()
        await fx.handles["conveyor"].set_target(ControlMode.DUTY, 0.3)
        fx.estop = True

        fx.refresher.clear_targets()
        fx.estop = False
        fx.clear_sent()
        await fx.refresher.step()

        assert fx.manager.sent == []


class TestClearSingleTarget:
    """`clear_target(name)` は 1 台だけを対象にする (`RobotServer._reenergize_motors` が使う)。

    `clear_targets()` (全台) と違い、同じバスで動作中の別モータを巻き込んで
    `wait_reached` を中断させてはならない。
    """

    async def test_only_the_named_handle_loses_its_target(self) -> None:
        fx = _Fixture()
        await fx.handles["conveyor"].set_target(ControlMode.DUTY, 0.3)
        await fx.handles["wall_f"].set_target(ControlMode.POSITION, 45.0)

        fx.refresher.clear_target("conveyor")

        assert fx.handles["conveyor"].has_target is False
        assert fx.handles["wall_f"].target == 45.0

    async def test_unknown_name_is_a_no_op(self) -> None:
        fx = _Fixture()
        await fx.handles["conveyor"].set_target(ControlMode.DUTY, 0.3)

        fx.refresher.clear_target("no_such_motor")

        assert fx.handles["conveyor"].target == 0.3


class TestPauseForMotorCheck:
    async def test_paused_refresher_sends_nothing(self) -> None:
        """動作確認は同じモータへ自前の指令を出す。古い目標を被せると誤判定になる。"""
        fx = _Fixture()
        await fx.handles["conveyor"].set_target(ControlMode.DUTY, 0.3)
        fx.clear_sent()

        await fx.refresher.pause(reason="動作確認")
        await fx.refresher.step()

        assert fx.manager.sent == []

    async def test_resume_restores_resending(self) -> None:
        fx = _Fixture()
        await fx.handles["conveyor"].set_target(ControlMode.DUTY, 0.3)
        await fx.refresher.pause(reason="動作確認")
        fx.refresher.resume()
        fx.clear_sent()

        await fx.refresher.step()

        assert fx.manager.names() == ["conveyor"]


class TestFailureIsolation:
    async def test_one_motor_failure_does_not_block_others(self) -> None:
        fx = _Fixture()
        await fx.handles["conveyor"].set_target(ControlMode.DUTY, 0.3)
        await fx.handles["wall_f"].set_target(ControlMode.POSITION, 90.0)
        fx.clear_sent()
        fx.manager.fail_motors = {"conveyor"}

        await fx.refresher.step()

        assert fx.manager.names() == ["wall_f"]

    async def test_run_survives_step_exception(self) -> None:
        """再送が止まると 500ms でモータが止まる。1 回の失敗でループを終わらせない。"""
        fx = _Fixture()
        await fx.handles["conveyor"].set_target(ControlMode.DUTY, 0.3)
        fx.manager.fail_motors = {"conveyor"}
        ticks = 0

        async def _sleep(_delay: float) -> None:
            nonlocal ticks
            ticks += 1
            if ticks >= 3:
                fx.refresher.request_stop()
            await asyncio.sleep(0)

        fx.refresher.set_sleep(_sleep)
        await fx.refresher.run()

        assert ticks == 3


class TestLifecycle:
    async def test_start_and_stop_leaves_no_task(self) -> None:
        fx = _Fixture()
        fx.refresher.start()
        assert fx.refresher.is_running is True

        await fx.refresher.stop()

        assert fx.refresher.is_running is False

    async def test_double_start_raises(self) -> None:
        fx = _Fixture()
        fx.refresher.start()
        try:
            with pytest.raises(RuntimeError):
                fx.refresher.start()
        finally:
            await fx.refresher.stop()

    async def test_stop_without_start_is_noop(self) -> None:
        fx = _Fixture()
        await fx.refresher.stop()
        assert fx.refresher.is_running is False


class TestWatchdogMargin:
    def test_interval_has_margin_over_firmware_watchdog(self) -> None:
        """ファームの猶予 500ms に対し、取りこぼしが数回続いても止まらない周期であること。"""
        assert DEFAULT_INTERVAL_S * 5 <= FIRMWARE_COMMAND_TIMEOUT_S


class _Dm3520Fixture:
    """DM3520 の再送タスク + モータ 1 台。"""

    def __init__(self, **driver_kwargs: object) -> None:
        self.manager = _StubCANManager()
        self.estop = False
        params: dict = {"master_id": 0x11}
        params.update(driver_kwargs)
        self.slide = Dm3520Driver("sub_slide", 0x05, **params)  # type: ignore[arg-type]
        self.handle = MotorHandle(
            "sub_slide",
            self.slide,
            self.manager,  # type: ignore[arg-type]
            is_estop_active=lambda: self.estop,
        )
        self.refresher = QueryDrivenTargetRefresher(
            [self.handle],
            self.manager,  # type: ignore[arg-type]
            is_estop_active=lambda: self.estop,
        )

    def clear_sent(self) -> None:
        self.manager.sent.clear()

    @staticmethod
    def position_of(msg: can.Message) -> float:
        p_des, _ = struct.unpack("<ff", msg.data)
        return p_des


class TestDm3520PollsEvenWithoutTarget:
    """**ここが自作モタドラと正反対**。送らないとフィードバックが 1 通も来ない。

    本機のフィードバックは問い合わせ駆動で、自分宛のフレームを受けたときにしか
    返らない。目標が無い間も送り続けないと、操縦していない時間はまるごと
    ``MotorHealth.STALE`` になり、症状は「常に赤い」だけで配線不良と区別が付かない。
    """

    async def test_sends_hold_target_before_any_command(self) -> None:
        fx = _Dm3520Fixture()
        feed_dm3520(fx.slide, position=1.5)
        fx.clear_sent()

        await fx.refresher.step()

        assert fx.manager.names() == ["sub_slide"]
        assert fx.position_of(fx.manager.sent[0][1]) == pytest.approx(1.5, abs=1e-3)

    async def test_hold_target_is_latched_not_re_measured(self) -> None:
        """**毎周期の実測角を書き直すとクリープする。**

        負荷で下がったぶんへ目標が追従していき、誰も操作していないのに軸が
        じりじり動く。ラッチした値を送り続けなければならない。
        """
        fx = _Dm3520Fixture()
        feed_dm3520(fx.slide, position=1.5)
        await fx.refresher.step()
        fx.clear_sent()

        # 負荷で 0.5rad ぶん下がった、という状況
        feed_dm3520(fx.slide, position=1.0)
        await fx.refresher.step()

        assert fx.position_of(fx.manager.sent[0][1]) == pytest.approx(1.5, abs=1e-3)

    async def test_velocity_mode_holds_stop(self) -> None:
        fx = _Dm3520Fixture(mode=ControlMode.VELOCITY)
        feed_dm3520(fx.slide, velocity=3.0)
        fx.clear_sent()

        await fx.refresher.step()

        assert struct.unpack("<f", fx.manager.sent[0][1].data)[0] == 0.0

    async def test_resends_the_operator_target_once_it_exists(self) -> None:
        fx = _Dm3520Fixture()
        await fx.handle.set_target(ControlMode.POSITION, 2.5)
        fx.clear_sent()

        await fx.refresher.step()

        assert fx.position_of(fx.manager.sent[0][1]) == pytest.approx(2.5)

    async def test_latch_is_retaken_after_the_target_is_cleared(self) -> None:
        """目標を失った時点の姿勢でラッチを取り直すこと。

        古いラッチが残っていると、次に励磁したときそこへ戻る動きが出る。
        """
        fx = _Dm3520Fixture()
        await fx.handle.set_target(ControlMode.POSITION, 2.5)
        await fx.refresher.step()

        fx.handle.clear_target()
        feed_dm3520(fx.slide, position=0.25)
        fx.clear_sent()
        await fx.refresher.step()

        assert fx.position_of(fx.manager.sent[0][1]) == pytest.approx(0.25, abs=1e-3)


class TestDm3520EStop:
    """停止中も送るが、送ってよいのは「今の姿勢を保て」だけ。"""

    async def test_keeps_polling_during_estop(self) -> None:
        """停止中に黙ると、停止した瞬間から画面が機体の状態を映さなくなる。

        送るのは実測角そのものなので、この経路で機構が動くことはない
        (励磁は enable でしか起きず、このタスクは enable を 1 通も送らない)。
        """
        fx = _Dm3520Fixture()
        feed_dm3520(fx.slide, position=1.0)
        fx.estop = True
        fx.clear_sent()

        await fx.refresher.step()

        assert fx.manager.names() == ["sub_slide"]
        assert fx.position_of(fx.manager.sent[0][1]) == pytest.approx(1.0, abs=1e-3)

    async def test_never_sends_enable(self) -> None:
        # 停止中に励磁フレームが 1 通でも出ると緊急停止が意味を失う
        fx = _Dm3520Fixture()
        await fx.handle.set_target(ControlMode.POSITION, 2.5)
        fx.estop = True
        fx.clear_sent()

        await fx.refresher.step()

        for _, msg in fx.manager.sent:
            assert bytes(msg.data)[-1] != 0xFC

    async def test_does_not_resend_the_pre_estop_target(self) -> None:
        """停止前の目標を送り続けると、解除して励磁した瞬間にそこへ戻る。"""
        fx = _Dm3520Fixture()
        await fx.handle.set_target(ControlMode.POSITION, 2.5)
        feed_dm3520(fx.slide, position=0.5)
        fx.estop = True
        fx.clear_sent()

        await fx.refresher.step()

        assert fx.position_of(fx.manager.sent[0][1]) == pytest.approx(0.5, abs=1e-3)

    async def test_does_not_latch_during_estop(self) -> None:
        """停止直後の惰走中にラッチすると、解除後 1 周期目にその位置へ戻す動きが出る。

        無励磁なのでクリープは起こり得ず、停止中は測り直してよい。
        """
        fx = _Dm3520Fixture()
        fx.estop = True
        feed_dm3520(fx.slide, position=1.0)
        await fx.refresher.step()

        feed_dm3520(fx.slide, position=0.2)
        fx.clear_sent()
        await fx.refresher.step()

        assert fx.position_of(fx.manager.sent[0][1]) == pytest.approx(0.2, abs=1e-3)

    async def test_clear_targets_drops_the_target(self) -> None:
        fx = _Dm3520Fixture()
        await fx.handle.set_target(ControlMode.POSITION, 2.5)

        fx.refresher.clear_targets()

        assert fx.handle.has_target is False


class TestDm3520ClearSingleTarget:
    """`clear_target(name)` は問い合わせ駆動のラッチ (`_idle_targets`) も剥がす。

    `RobotServer._reenergize_motors` が使う経路。剥がさずに励磁すると、
    直後の再送 (最大 20Hz = 50ms 後) がフォルト前の古いラッチで上書きし、
    「現在角を書いてから励磁する」保証が 1 周期で無効になる。
    """

    def _two_motor_fixture(self) -> tuple[_StubCANManager, dict[str, MotorHandle], object]:
        manager = _StubCANManager()
        dropped = Dm3520Driver("dropped", 0x01, master_id=0x11)
        healthy = Dm3520Driver("healthy", 0x02, master_id=0x12)
        handles = {
            driver.name: MotorHandle(driver.name, driver, manager)  # type: ignore[arg-type]
            for driver in (dropped, healthy)
        }
        refresher = QueryDrivenTargetRefresher(
            list(handles.values()),
            manager,
            is_estop_active=lambda: False,  # type: ignore[arg-type]
        )
        return manager, handles, refresher

    async def test_named_motor_relatches_fresh_position(self) -> None:
        manager, handles, refresher = self._two_motor_fixture()
        dropped_driver = handles["dropped"].driver
        healthy_driver = handles["healthy"].driver

        # 目標を持たないまま「今の姿勢を保て」がラッチされる
        feed_dm3520(dropped_driver, position=3.0)  # type: ignore[arg-type]
        feed_dm3520(healthy_driver, position=0.0)  # type: ignore[arg-type]
        await refresher.step()

        # フォルトで機構が沈んだ (1.0)。ラッチ (3.0) はまだ古いまま
        feed_dm3520(dropped_driver, position=1.0)  # type: ignore[arg-type]

        refresher.clear_target("dropped")
        manager.sent.clear()
        await refresher.step()

        sent = {name: msg for name, msg in manager.sent}
        p_des, _ = struct.unpack("<ff", sent["dropped"].data)
        assert p_des == pytest.approx(1.0, abs=1e-3)

    async def test_other_motor_on_same_refresher_keeps_its_latch(self) -> None:
        manager, handles, refresher = self._two_motor_fixture()
        dropped_driver = handles["dropped"].driver
        healthy_driver = handles["healthy"].driver

        feed_dm3520(dropped_driver, position=3.0)  # type: ignore[arg-type]
        feed_dm3520(healthy_driver, position=2.0)  # type: ignore[arg-type]
        await refresher.step()

        # healthy の負荷が変わっても、剥がしていない側のラッチは追従しない
        feed_dm3520(healthy_driver, position=0.5)  # type: ignore[arg-type]

        refresher.clear_target("dropped")
        manager.sent.clear()
        await refresher.step()

        sent = {name: msg for name, msg in manager.sent}
        p_des, _ = struct.unpack("<ff", sent["healthy"].data)
        assert p_des == pytest.approx(2.0, abs=1e-3)


class TestDm3520PauseForMotorCheck:
    async def test_paused_refresher_sends_nothing(self) -> None:
        """動作確認は同じモータへ自前の指令を出す。並行して送ると打ち消し合う。"""
        fx = _Dm3520Fixture()
        feed_dm3520(fx.slide, position=1.0)
        await fx.refresher.pause(reason="動作確認")
        fx.clear_sent()

        await fx.refresher.step()

        assert fx.manager.sent == []

    async def test_resume_restores_sending(self) -> None:
        fx = _Dm3520Fixture()
        feed_dm3520(fx.slide, position=1.0)
        await fx.refresher.pause(reason="動作確認")
        fx.refresher.resume()
        fx.clear_sent()

        await fx.refresher.step()

        assert fx.manager.names() == ["sub_slide"]


class TestDm3520FailureIsolation:
    async def test_send_failure_does_not_escape(self) -> None:
        """1 台の送信失敗でループを降りると、そのモータは永久に STALE のまま。"""
        fx = _Dm3520Fixture()
        feed_dm3520(fx.slide, position=1.0)
        fx.manager.fail_motors.add("sub_slide")

        await fx.refresher.step()  # 例外が漏れないこと

        assert fx.manager.sent == []
