from __future__ import annotations

import asyncio
import logging
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import can
import pytest

from lib.can_manager import _RECV_RETRY_MIN_S, _RX_BATCH_MAX, CANManager
from lib.drivers.base import ControlMode, MotorState
from lib.drivers.generic import GenericDriver
from lib.drivers.m3508 import M3508Driver
from lib.health import BusHealth
from tests.fake_can import (
    ReadableBus,
    direct_runner,
    mark_feedback_at,
    mock_bus,
    mock_driver,
)
from tests.feedback_frames import generic_feedback, m3508_feedback


class TestCANManager:
    def test_add_bus_and_motor(self) -> None:
        mgr = CANManager()
        bus = mock_bus()
        motor = mock_driver("m1", 1)

        mgr.add_bus("can0", bus)
        mgr.add_motor("can0", motor)

        assert mgr.motors["m1"] is motor

    def test_unknown_motor_is_not_registered(self) -> None:
        mgr = CANManager()
        assert "nonexistent" not in mgr.motors

    async def test_send_to_correct_bus(self) -> None:
        calls: list[tuple[Any, tuple[Any, ...]]] = []
        mgr = CANManager(run_blocking=direct_runner(calls))
        bus0 = mock_bus()
        bus1 = mock_bus()
        motor = mock_driver("m1", 1)

        mgr.add_bus("can0", bus0)
        mgr.add_bus("can1", bus1)
        mgr.add_motor("can0", motor)

        msg = can.Message(arbitration_id=0x200, data=bytes(8))
        await mgr.send("m1", msg)

        assert calls == [(bus0.send, (msg,))]

    async def test_initialize_motors_sends_steps_with_declared_delays(self) -> None:
        mgr = CANManager()
        bus = mock_bus()
        motor = mock_driver("m1", 1)
        first = can.Message(arbitration_id=0x201, data=bytes(8))
        second = can.Message(arbitration_id=0x202, data=bytes(8))
        motor.initialization_steps.return_value = [(first, 0.05), (second, 0.1)]
        mgr.add_bus("can0", bus)
        mgr.add_motor("can0", motor)

        with (
            patch.object(mgr, "send", new_callable=AsyncMock) as send,
            patch("lib.can_manager.asyncio.sleep", new_callable=AsyncMock) as sleep,
        ):
            await mgr.initialize_motors()

        assert send.await_args_list[0].args == ("m1", first)
        assert send.await_args_list[1].args == ("m1", second)
        assert [call.args[0] for call in sleep.await_args_list] == [0.05, 0.1]

    async def test_run_initializes_motors_after_starting_receivers(self) -> None:
        mgr = CANManager()
        mgr.add_bus("can0", mock_bus())

        with patch.object(mgr, "initialize_motors", new_callable=AsyncMock) as initialize_motors:
            await mgr.run()

        initialize_motors.assert_awaited_once_with()
        assert len(mgr._tasks) == 1
        await mgr.shutdown()

    async def test_run_は有効化できなかったモータ名を返す(self) -> None:
        """**起動時の励磁失敗はここ以外に現れる場所が無い。**

        捨てると `safety.unenergized_motors` は緊急停止解除の経路でしか埋まらず、
        操縦者に見えるのは「指令しても動かない」だけになる。
        """
        mgr = CANManager()
        mgr.add_bus("can0", mock_bus())

        with patch.object(mgr, "initialize_motors", new_callable=AsyncMock, return_value=["m1"]):
            inactive = await mgr.run()

        assert inactive == ["m1"]
        await mgr.shutdown()

    async def test_receive_updates_motor_state(self) -> None:
        mgr = CANManager(run_blocking=direct_runner())
        bus = mock_bus()
        motor = mock_driver("m1", 1)
        motor.matches_feedback.return_value = True

        feedback_state = MotorState(position=90.0, velocity=100.0)
        motor.update_state.return_value = feedback_state

        mgr.add_bus("can0", bus)
        mgr.add_motor("can0", motor)

        feedback_msg = can.Message(arbitration_id=0x201, data=bytes(8))

        call_count = 0

        def recv_side_effect(timeout: float) -> can.Message | None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return feedback_msg
            raise asyncio.CancelledError

        bus.recv.side_effect = recv_side_effect

        with pytest.raises(asyncio.CancelledError):
            await mgr._receive_loop("can0")

        motor.matches_feedback.assert_called_once_with(feedback_msg)
        motor.update_state.assert_called_once_with(feedback_msg)

    async def test_shutdown(self) -> None:
        mgr = CANManager()
        bus0 = mock_bus()
        bus1 = mock_bus()

        mgr.add_bus("can0", bus0)
        mgr.add_bus("can1", bus1)

        await mgr.shutdown()

        bus0.shutdown.assert_called_once()
        bus1.shutdown.assert_called_once()

    async def test_shutdown_は受信し続けているバスも畳んで全バスを閉じる(self) -> None:
        """止める処理が止まってはならない。

        **かつてはここで「バスが down していると受信ループは即死する」ことを
        前提にしていた。** いまは降りずに再試行を続けるので、片方が再試行中でも
        `shutdown()` が両方のバスを閉じ切ることを見る。畳めないタスクが 1 つでも
        あると、`main()` の finally がそこで折れて 2 台目のバスが開いたまま残る。
        """
        # 既定のエグゼキュータ経由の runner を使う。同期実行の runner だと
        # 正常な方のバスの受信ループがイベントループへ譲らず回り続けてしまう
        mgr = CANManager()
        bus0 = mock_bus()
        bus0.recv.side_effect = can.CanOperationError("インタフェース断")
        bus1 = mock_bus()
        mgr.add_bus("can0", bus0)
        mgr.add_bus("can1", bus1)

        await mgr.run()
        # 再試行のバックオフに入っているタイミングで畳む
        await asyncio.sleep(0.03)
        await mgr.shutdown()

        bus0.shutdown.assert_called_once()
        bus1.shutdown.assert_called_once()

    async def test_shutdown_は既に死んでいる受信タスクの例外で止まらない(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """受信ループが降りない今も、この防護は単独で効いている必要がある。

        受信ループ側で握るようになったぶん、ここが壊れても普段は誰も気付けない。
        層を 1 枚ずつ確かめる原則に従い、**既に例外で終わったタスク**を直接
        持たせて、`shutdown()` がそれを握って残りのバスを閉じ切ることを見る。
        """

        async def _die() -> None:
            raise RuntimeError("受信ループが想定外の理由で降りた")

        mgr = CANManager()
        bus0 = mock_bus()
        mgr.add_bus("can0", bus0)

        dead = asyncio.create_task(_die())
        await asyncio.sleep(0)
        mgr._tasks.append(dead)

        with caplog.at_level(logging.ERROR, logger="lib.can_manager"):
            await mgr.shutdown()

        bus0.shutdown.assert_called_once()

    async def test_shutdown_は1本のバス停止失敗で残りを諦めない(self) -> None:
        mgr = CANManager()
        bus0 = mock_bus()
        bus0.shutdown.side_effect = RuntimeError("デバイスが既に外れている")
        bus1 = mock_bus()
        mgr.add_bus("can0", bus0)
        mgr.add_bus("can1", bus1)

        await mgr.shutdown()

        bus1.shutdown.assert_called_once()


class TestMotorActivation:
    """励磁の有効化は「有効化した瞬間に動かない」ことを保証してからでないと行えない。"""

    def _prepare(self) -> tuple[CANManager, MagicMock]:
        mgr = CANManager()
        mgr.add_bus("can0", mock_bus())
        motor = mock_driver("m1", 1)
        mgr.add_motor("can0", motor)
        return mgr, motor

    async def test_initialize_motors_activates_after_initialization_steps(self) -> None:
        mgr, motor = self._prepare()
        init_msg = can.Message(arbitration_id=0x201, data=bytes(8))
        enable_msg = can.Message(arbitration_id=0x202, data=bytes(8))
        motor.initialization_steps.return_value = [(init_msg, 0.0)]
        motor.activation_steps.return_value = [(enable_msg, 0.0)]

        with patch.object(mgr, "send", new_callable=AsyncMock) as send:
            await mgr.initialize_motors()

        assert [call.args[1] for call in send.await_args_list] == [init_msg, enable_msg]

    async def test_設定と励磁はモータ単位で交互に送る(self) -> None:
        """「全モータの設定 → 全モータの励磁」に組み替えてはならない。

        EDULITE 05 / DM3520 は activation_steps を組み立てるときに実測角を読み、
        それを目標として書いてから励磁する。並べ替えると、読む実測角が
        「自分の設定を送った直後」ではなく「他機の設定を挟んだ後」のものになる。
        """
        mgr = CANManager()
        mgr.add_bus("can0", mock_bus())
        msgs = {}
        for name, base in (("m1", 0x210), ("m2", 0x220)):
            motor = mock_driver(name, base & 0xFF)
            msgs[f"{name}_init"] = can.Message(arbitration_id=base, data=bytes(8))
            msgs[f"{name}_enable"] = can.Message(arbitration_id=base + 1, data=bytes(8))
            motor.initialization_steps.return_value = [(msgs[f"{name}_init"], 0.0)]
            motor.activation_steps.return_value = [(msgs[f"{name}_enable"], 0.0)]
            mgr.add_motor("can0", motor)

        with patch.object(mgr, "send", new_callable=AsyncMock) as send:
            await mgr.initialize_motors()

        assert [call.args[1] for call in send.await_args_list] == [
            msgs["m1_init"],
            msgs["m1_enable"],
            msgs["m2_init"],
            msgs["m2_enable"],
        ]

    async def test_activation_reads_position_after_fresh_feedback_arrives(self) -> None:
        """set_zero 後の原点を反映した実測角でなければ、目標として書いてはいけない。"""
        mgr, motor = self._prepare()
        motor.requires_fresh_feedback_for_activation.return_value = True
        motor.feedback_probe_message.return_value = can.Message(arbitration_id=0x203, data=bytes(8))
        enable_msg = can.Message(arbitration_id=0x202, data=bytes(8))

        # 待機開始前の受信は set_zero 前の可能性があるため、認めてはならない
        mark_feedback_at(mgr, "m1", time.time())

        seen_rx_at: list[float | None] = []

        def record_activation() -> list[tuple[can.Message, float]]:
            seen_rx_at.append(mgr.last_feedback_at("m1"))
            return [(enable_msg, 0.0)]

        motor.activation_steps.side_effect = record_activation

        async def fake_send(name: str, msg: can.Message) -> None:
            # 問い合わせフレームへの応答としてフィードバックが届く状況を模す
            mark_feedback_at(mgr, name, time.time())

        with patch.object(mgr, "send", new_callable=AsyncMock, side_effect=fake_send):
            activated = await mgr.activate_motor("m1", feedback_timeout_s=0.5)

        assert activated is True
        assert seen_rx_at and seen_rx_at[0] is not None
        assert seen_rx_at[0] > (mgr.last_feedback_at("m1") or 0.0) - 1.0

    async def test_activation_skipped_when_feedback_never_arrives(self) -> None:
        """現在角が分からないまま enable すると原点へ飛ぶため、無励磁のままにする。"""
        mgr, motor = self._prepare()
        motor.requires_fresh_feedback_for_activation.return_value = True
        motor.activation_steps.return_value = [
            (can.Message(arbitration_id=0x202, data=bytes(8)), 0.0)
        ]

        with patch.object(mgr, "send", new_callable=AsyncMock) as send:
            activated = await mgr.activate_motor("m1", feedback_timeout_s=0.05)

        assert activated is False
        motor.activation_steps.assert_not_called()
        assert send.await_count == 0

    async def test_activation_requires_feedback_newer_than_wait_start(self) -> None:
        mgr, motor = self._prepare()
        motor.requires_fresh_feedback_for_activation.return_value = True
        motor.activation_steps.return_value = [
            (can.Message(arbitration_id=0x202, data=bytes(8)), 0.0)
        ]
        mark_feedback_at(mgr, "m1", time.time())

        with patch.object(mgr, "send", new_callable=AsyncMock):
            activated = await mgr.activate_motor("m1", feedback_timeout_s=0.05)

        assert activated is False
        motor.activation_steps.assert_not_called()

    async def test_activate_motors_stops_when_abort_requested(self) -> None:
        """緊急停止が再び入ったら、途中でも enable を送ってはならない。"""
        mgr, motor = self._prepare()
        motor.activation_steps.return_value = [
            (can.Message(arbitration_id=0x202, data=bytes(8)), 0.0)
        ]

        with patch.object(mgr, "send", new_callable=AsyncMock) as send:
            inactive = await mgr.activate_motors(should_abort=lambda: True)

        assert send.await_count == 0
        # 中断で飛ばしたモータも「励磁できていない」として報告する
        assert inactive == ["m1"]

    async def test_activate_motors_continues_after_one_motor_fails(self) -> None:
        """**1 台の送信失敗で残りのモータの有効化を諦めてはならない。**

        緊急停止の原因がそのまま送信失敗を招く場面がある —— 専用バスに 1 台しか
        居ない DM3520 が電源を失うと ACK が返らず、そのバスの送信は全滅する。
        素の for に並べると最初のモータの例外で以降へ enable が 1 通も飛ばず、
        しかも `RobotServer._reactivate_motors` はそれをログに落とすだけなので、
        画面は「解除できた」ように見えたまま機体が無励磁で取り残される。
        """
        mgr = CANManager()
        mgr.add_bus("can0", mock_bus())
        enable_msg = can.Message(arbitration_id=0x202, data=bytes(8))
        for index, name in enumerate(("m1", "m2", "m3"), start=1):
            motor = mock_driver(name, index)
            motor.activation_steps.return_value = [(enable_msg, 0.0)]
            mgr.add_motor("can0", motor)

        async def fail_first(name: str, msg: can.Message) -> None:
            if name == "m1":
                raise can.CanError("ACK が返らない")

        with patch.object(mgr, "send", new_callable=AsyncMock, side_effect=fail_first) as send:
            inactive = await mgr.activate_motors()

        assert inactive == ["m1"]
        assert [call.args[0] for call in send.await_args_list] == ["m1", "m2", "m3"]

    async def test_initialize_motors_continues_after_one_motor_fails(self) -> None:
        """起動時も同じ。1 台の失敗でそのバスのモータが全部無励磁になってはならない。"""
        mgr = CANManager()
        mgr.add_bus("can0", mock_bus())
        msg = can.Message(arbitration_id=0x202, data=bytes(8))
        for index, name in enumerate(("m1", "m2"), start=1):
            motor = mock_driver(name, index)
            motor.initialization_steps.return_value = [(msg, 0.0)]
            motor.activation_steps.return_value = [(msg, 0.0)]
            mgr.add_motor("can0", motor)

        async def fail_first(name: str, _msg: can.Message) -> None:
            if name == "m1":
                raise can.CanError("ACK が返らない")

        with patch.object(mgr, "send", new_callable=AsyncMock, side_effect=fail_first) as send:
            inactive = await mgr.initialize_motors()

        assert inactive == ["m1"]
        assert "m2" in [call.args[0] for call in send.await_args_list]


class TestClearEStopLatches:
    """ラッチ解除は「励磁」ではないので、対象も中断の作法も励磁とは別物になる。"""

    def _manager(self) -> tuple[CANManager, MagicMock]:
        mgr = CANManager(run_blocking=direct_runner())
        bus = mock_bus()
        mgr.add_bus("can0", bus)
        return mgr, bus

    async def test_自作モタドラ以外へは1通も送らない(self) -> None:
        """EDULITE 05 / DM3520 の励磁は従来どおり中断ありの経路に残すこと。

        こちらのフェーズは中断されないので、本当に励磁するドライバを混ぜると
        「緊急停止が再発動しているのに機体が励磁される」経路ができる。
        """
        mgr, bus = self._manager()
        board = GenericDriver("board", 0x11, control_type=ControlMode.DUTY)
        energized = mock_driver("arm", 0x21)
        energized.activation_steps.return_value = [
            (can.Message(arbitration_id=0x123, data=bytes(8), is_extended_id=False), 0.0)
        ]
        mgr.add_motor("can0", board)
        mgr.add_motor("can0", energized)

        uncleared = await mgr.clear_e_stop_latches()

        assert uncleared == []
        sent = [call.args[0] for call in bus.send.call_args_list]
        assert [msg.arbitration_id for msg in sent] == [
            GenericDriver.encode_e_stop_clear(0x11).arbitration_id
        ]
        energized.activation_steps.assert_not_called()

    async def test_ブロードキャストではなく個別の宛先へ送る(self) -> None:
        """共有バス上の他ロボットのラッチまで巻き添えで外さないこと。"""
        mgr, bus = self._manager()
        mgr.add_motor("can0", GenericDriver("board", 0x11, control_type=ControlMode.DUTY))

        await mgr.clear_e_stop_latches()

        sent = bus.send.call_args_list[0].args[0]
        expected = GenericDriver.encode_e_stop_clear(0x11)
        assert sent.arbitration_id == expected.arbitration_id
        assert bytes(sent.data) == bytes(expected.data)

    async def test_1台の送信失敗で残りを諦めない(self) -> None:
        """緊急停止の原因がそのまま送信失敗を招いている場面が本番そのものである。"""
        mgr, bus = self._manager()
        mgr.add_motor("can0", GenericDriver("first", 0x11, control_type=ControlMode.DUTY))
        mgr.add_motor("can0", GenericDriver("second", 0x12, control_type=ControlMode.DUTY))
        bus.send.side_effect = [can.CanError("ACK が返らない"), None]

        uncleared = await mgr.clear_e_stop_latches()

        assert uncleared == ["first"]
        assert bus.send.call_count == 2


class TestReceiveLoopRobustness:
    """受信ループは想定外のフレーム 1 通で死んではならない。

    死ぬとそのバスの全モータが永久に STALE になり、試合中は復旧不能になる。
    """

    @staticmethod
    def _drain_recv(bus: MagicMock, messages: list[can.Message]) -> None:
        queue = list(messages)

        def recv_side_effect(timeout: float) -> can.Message | None:
            if queue:
                return queue.pop(0)
            raise asyncio.CancelledError

        bus.recv.side_effect = recv_side_effect

    async def _run_loop(self, mgr: CANManager) -> None:
        with pytest.raises(asyncio.CancelledError):
            await mgr._receive_loop("can0")

    @pytest.mark.parametrize("reserved_command_type", [0b101, 0b110, 0b111])
    async def test_receive_loop_survives_reserved_command_type(
        self, reserved_command_type: int
    ) -> None:
        mgr = CANManager(run_blocking=direct_runner())
        bus = mock_bus()
        motor = GenericDriver("gripper", 0x01)
        mgr.add_bus("can0", bus)
        mgr.add_motor("can0", motor)

        bogus = can.Message(
            arbitration_id=(reserved_command_type << 8) | 0x01,
            data=bytes(8),
            is_extended_id=False,
        )
        self._drain_recv(bus, [bogus, generic_feedback(motor, position=90.0)])

        await self._run_loop(mgr)

        # 予約種別のフレームの後でも、続くフィードバックが取り込めていること
        assert motor.state.position == pytest.approx(90.0)

    async def test_receive_loop_survives_extended_frame(self) -> None:
        mgr = CANManager(run_blocking=direct_runner())
        bus = mock_bus()
        motor = GenericDriver("gripper", 0x01)
        mgr.add_bus("can0", bus)
        mgr.add_motor("can0", motor)

        alien = can.Message(arbitration_id=0x12345678, data=bytes(8), is_extended_id=True)
        self._drain_recv(bus, [alien, generic_feedback(motor, position=45.0)])

        await self._run_loop(mgr)

        assert motor.state.position == pytest.approx(45.0)

    async def test_receive_loop_survives_short_m3508_frame(self) -> None:
        """M3508 は DLC を検査せずに struct.unpack する。短いフレームは実際に届く。

        M3508Driver.matches_feedback は arbitration_id しか見ないため、
        バス上の別機器が 0x201〜0x204 を 8 バイト未満で流すだけで decode が落ちる。
        """
        mgr = CANManager(run_blocking=direct_runner())
        bus = mock_bus()
        hit = M3508Driver("y_axis_r", 1)
        other = M3508Driver("y_axis_l", 2)
        mgr.add_bus("can0", bus)
        mgr.add_motor("can0", hit)
        mgr.add_motor("can0", other)

        short = can.Message(arbitration_id=0x201, data=bytes(4), is_extended_id=False)
        self._drain_recv(bus, [short, m3508_feedback(other, angle_raw=2048)])

        await self._run_loop(mgr)

        # 巻き添えを受けずに、後続の正常フレームが取り込めていること
        assert other.state.position == pytest.approx(90.0, abs=0.1)
        assert mgr.last_feedback_at("y_axis_l") is not None
        # デコードできなかった以上、当該モータは「受信した」ことにしてはならない
        assert mgr.last_feedback_at("y_axis_r") is None
        assert mgr._rx_error_count["can0"] == 1

    async def test_receive_loop_isolates_failing_matcher_to_one_motor(self) -> None:
        """matches_feedback が投げるドライバが、同じバスの他モータ宛を巻き添えにしない。"""
        mgr = CANManager(run_blocking=direct_runner())
        bus = mock_bus()
        broken = mock_driver("broken", 0x02)
        broken.matches_feedback.side_effect = ValueError("解析できない ID")
        healthy = GenericDriver("gripper", 0x01)
        mgr.add_bus("can0", bus)
        mgr.add_motor("can0", broken)
        mgr.add_motor("can0", healthy)

        self._drain_recv(bus, [generic_feedback(healthy, position=90.0)])

        await self._run_loop(mgr)

        # 同じ 1 通が、壊れたドライバの後ろにいる健全なモータへ届いていること
        assert healthy.state.position == pytest.approx(90.0)
        assert mgr._rx_error_count["can0"] == 1

    async def test_receive_loop_survives_interface_down_and_resumes(self) -> None:
        """**インタフェース断で受信ループを終わらせてはならない。**

        `ip link set down` / CANable の抜き差し / `setup_can.sh` の再実行はいずれも
        `bus.recv` を `Network is down` で失敗させるが、どれも 1 秒以内に戻る一過性の
        事象である。ここで降りると、**送信側だけが自動復帰して受信は二度と戻らない**
        —— 症状は「指令は効くのにフィードバックだけ永久に無い」で、機体は動くのに
        全モータが STALE のまま試合を終える。実際にこれで 1 回沈黙した。

        socketcan のソケットは down/up をまたいでも生き続けるので (実測済み)、
        同じ Bus のまま recv を再試行するだけで復帰する。
        """
        mgr = CANManager(run_blocking=direct_runner())
        bus = mock_bus()
        motor = GenericDriver("gripper", 0x01)
        mgr.add_bus("can0", bus)
        mgr.add_motor("can0", motor)

        queue: list[can.Message | Exception] = [
            can.CanOperationError("Error receiving: Network is down [Error Code 100]"),
            generic_feedback(motor, position=90.0),
        ]

        def recv_side_effect(timeout: float) -> can.Message | None:
            if not queue:
                raise asyncio.CancelledError
            item = queue.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        bus.recv.side_effect = recv_side_effect

        await self._run_loop(mgr)

        # 断のあとに届いた 1 通が、ちゃんとモータへ配られていること
        assert motor.state.position == pytest.approx(90.0)
        assert mgr.last_feedback_at("gripper") is not None

    async def test_receive_loop_backs_off_after_a_receive_failure(self) -> None:
        """復帰を待つ間、全速で再試行してはならない。

        インタフェースが戻らない場合、失敗は同じ速さで繰り返される。素の
        ``continue`` だと 1 コアを食い潰したままログを溢れさせ、**同じプロセスに
        同居している位置制御ループ (200Hz) と偏差監視 (50Hz) の周期まで
        巻き添えにする**。

        ``asyncio.sleep`` を patch して回数を数える書き方は採らない ——
        ``lib.can_manager.asyncio`` は共有のモジュールオブジェクトなので、
        patch するとプロセス全体の ``asyncio.sleep`` が差し替わり、
        pytest-asyncio ごと巻き添えにしてテストセッションが停止する
        (実際にこれでハングさせた)。実時間で「呼ばれた回数」を測れば、
        待っていることは外から確かめられる。

        待ち時間は失敗のたびに伸びる (`_RECV_RETRY_MIN_S` から `_RECV_RETRY_MAX_S`
        まで) ので、最短の間隔で回り続けた場合を上限として見る。
        """
        mgr = CANManager(run_blocking=direct_runner())
        bus = mock_bus()
        mgr.add_bus("can0", bus)
        bus.recv.side_effect = can.CanOperationError("Network is down")

        window_s = _RECV_RETRY_MIN_S * 15
        task = asyncio.create_task(mgr._receive_loop("can0"))
        await asyncio.sleep(window_s)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # 待たずに回すと、この窓のあいだに数千回の recv が走る
        spin_free_limit = int(window_s / _RECV_RETRY_MIN_S) + 2
        assert bus.recv.call_count <= spin_free_limit, (
            f"失敗のたびに待たずに再試行している (recv 呼び出し {bus.recv.call_count} 回)"
        )
        assert bus.recv.call_count >= 1, "1 度も再試行していない"

    async def test_receive_loop_propagates_cancelled_error(self) -> None:
        """CancelledError は shutdown の停止経路。握り潰すと止まらない受信ループが残る。"""
        mgr = CANManager(run_blocking=direct_runner())
        bus = mock_bus()

        class CancellingDriver(GenericDriver):
            """デコードの途中で停止要求が入った状況を作る。

            受信ループはドライバ呼び出しを try で囲んで例外を握り潰すので、
            そこで CancelledError まで飲み込むと shutdown が効かなくなる。
            """

            def update_state(self, msg: can.Message) -> MotorState:
                raise asyncio.CancelledError

        motor = CancellingDriver("gripper", 0x01)
        mgr.add_bus("can0", bus)
        mgr.add_motor("can0", motor)
        self._drain_recv(
            bus, [generic_feedback(motor, position=90.0), generic_feedback(motor, position=45.0)]
        )

        await self._run_loop(mgr)

        # 1 通目で抜けている (2 通目を読みに行っていない) こと
        assert bus.recv.call_count == 1
        # 停止要求は受信エラーではない
        assert mgr._rx_error_count["can0"] == 0

    async def test_受信断は降りずに必ず記録される(self, caplog: pytest.LogCaptureFixture) -> None:
        """受信 API 自体の失敗は、痕跡を残したうえで**再試行する**。

        **かつてはここで「降りるときは必ず痕跡を残す」ことを見ていた。**
        降りなくなったぶん、黙って再試行し続けるのが最も危ない失敗になった ——
        ログにも UI にも出ないまま、そのバスの全モータが STALE になる。

        降りないことも同時に見る。降りると送信側だけが次の周期で自動復帰し、
        受信は二度と戻らない。症状は「指令は効くのにフィードバックだけ永久に無い」で、
        機体は動くのに全モータが STALE のまま試合を終える (実機で発生済み)。
        """
        mgr = CANManager()
        bus = mock_bus()
        bus.recv.side_effect = can.CanOperationError("インタフェース断")
        mgr.add_bus("can0", bus)
        mgr.add_motor("can0", GenericDriver("gripper", 0x01))

        with caplog.at_level(logging.ERROR, logger="lib.can_manager"):
            task = asyncio.create_task(mgr._receive_loop("can0"))
            await asyncio.sleep(0.05)
            still_running = not task.done()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert still_running, "受信 API の失敗でループが降りている"
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert errors, "受信断がどこにも記録されていない"
        assert any(r.exc_info is not None for r in errors), "トレースバックが残っていない"

    async def test_receive_error_logs_are_throttled(self, caplog: pytest.LogCaptureFixture) -> None:
        """不正フレームが連続しても、1 通ごとにログを出すと他のログが読めなくなる。"""
        mgr = CANManager(run_blocking=direct_runner())
        bus = mock_bus()
        mgr.add_bus("can0", bus)
        mgr.add_motor("can0", M3508Driver("y_axis_r", 1))

        short = can.Message(arbitration_id=0x201, data=bytes(4), is_extended_id=False)
        self._drain_recv(bus, [short] * 5)

        with caplog.at_level(logging.ERROR, logger="lib.can_manager"):
            await self._run_loop(mgr)

        assert mgr._rx_error_count["can0"] == 5
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert len(errors) == 1
        # トレースバックが無いと、どのデコードで落ちたか追えない
        assert errors[0].exc_info is not None


class TestDuplicateRegistration:
    """名前 / CAN ID の重複はフィードバックの配り先を静かに壊すため構成時に弾く。"""

    def test_duplicate_motor_name_is_rejected(self) -> None:
        mgr = CANManager()
        mgr.add_bus("can_generic", mock_bus())
        mgr.add_motor("can_generic", mock_driver("gripper", 0x01))

        with pytest.raises(ValueError) as excinfo:
            mgr.add_motor("can_generic", mock_driver("gripper", 0x02))

        assert "gripper" in str(excinfo.value)

    def test_duplicate_motor_name_across_buses_is_rejected(self) -> None:
        """名前は _motors の唯一のキーなので、別バスでも後勝ちで上書きされてしまう。"""
        mgr = CANManager()
        mgr.add_bus("can_generic", mock_bus())
        mgr.add_bus("can_edulite", mock_bus())
        mgr.add_motor("can_generic", mock_driver("gripper", 0x01))

        with pytest.raises(ValueError):
            mgr.add_motor("can_edulite", mock_driver("gripper", 0x01))

    def test_duplicate_can_id_on_same_bus_is_rejected(self) -> None:
        mgr = CANManager()
        mgr.add_bus("can_generic", mock_bus())
        mgr.add_motor("can_generic", mock_driver("gripper", 0x01))

        with pytest.raises(ValueError) as excinfo:
            mgr.add_motor("can_generic", mock_driver("wall", 0x01))

        message = str(excinfo.value)
        # どのバスの・どの CAN ID が・どのモータと衝突したかが分からないと現物を追えない
        assert "can_generic" in message
        assert "0x01" in message
        assert "gripper" in message
        assert "wall" in message

    def test_same_can_id_on_different_bus_is_allowed(self) -> None:
        """バスが違えばフレームは混ざらない。ここまで弾くと現実の配線が組めない。"""
        mgr = CANManager()
        mgr.add_bus("can_generic", mock_bus())
        mgr.add_bus("can_edulite", mock_bus())
        mgr.add_motor("can_generic", mock_driver("gripper", 0x01))
        mgr.add_motor("can_edulite", mock_driver("rotate_l", 0x01))

        assert mgr.motors["gripper"].can_id == mgr.motors["rotate_l"].can_id

    def test_rejected_motor_is_not_registered(self) -> None:
        """弾いた後に _bus_motors 側だけ残ると、受信ループが孤児へフレームを配る。"""
        mgr = CANManager()
        mgr.add_bus("can_generic", mock_bus())
        first = mock_driver("gripper", 0x01)
        mgr.add_motor("can_generic", first)

        with pytest.raises(ValueError):
            mgr.add_motor("can_generic", mock_driver("wall", 0x01))

        assert mgr._bus_motors["can_generic"] == [first]
        assert set(mgr._motors) == {"gripper"}


class TestReadOnlyViews:
    """構成の読み取り口。サーバー・動作確認がここを通れば private を触らずに済む。"""

    def _mgr(self) -> CANManager:
        mgr = CANManager()
        mgr.add_bus("can_m3508", mock_bus(), channel="vcan0")
        mgr.add_bus("can_generic", mock_bus(), channel="vcan1")
        mgr.add_motor("can_m3508", mock_driver("y_axis_r", 0x01))
        mgr.add_motor("can_m3508", mock_driver("y_axis_l", 0x02))
        mgr.add_motor("can_generic", mock_driver("gripper", 0x01))
        return mgr

    def test_motors_は宣言順を保つ(self) -> None:
        # 動作確認は config の宣言順に 1 台ずつ動かす。順序が崩れると
        # 指差喚呼の読み上げ順と画面の進捗が食い違う
        mgr = self._mgr()
        assert list(mgr.motors) == ["y_axis_r", "y_axis_l", "gripper"]

    def test_motors_は書き換えられない(self) -> None:
        mgr = self._mgr()
        with pytest.raises(TypeError):
            mgr.motors["gripper"] = mock_driver("gripper", 0x09)  # type: ignore[index]

    def test_motors_は登録を追従する(self) -> None:
        mgr = self._mgr()
        view = mgr.motors
        mgr.add_motor("can_generic", mock_driver("wall", 0x02))
        assert "wall" in view

    def test_bus_names_で送信先バスを列挙できる(self) -> None:
        mgr = self._mgr()
        assert mgr.bus_names == ("can_m3508", "can_generic")


class TestReceiveLoopSurvivesInterfaceDown:
    """受信の断絶で降りないこと、降りないことが黙殺にならないこと。

    `scripts/can_watchdog.sh` は bus-off 復旧のたびに `ip link` の down/up を出す。
    その約 1 秒のあいだ `bus.recv` は `Network is down` で失敗し続けるが、
    **同一 socket は down/up を跨いで生き残る** (実測) ので、待って呼び直せば戻る。

    かつてはここで受信タスクごと降りていた。``_tasks`` は誰も await しないため死は
    ログ 1 行にしか現れず、症状は「UI は接続中のまま全モータが STALE」という
    最も切り分けにくい形になっていた。
    """

    async def test_インタフェース断で降りず復帰後に受信を再開する(self) -> None:
        mgr = CANManager()
        bus = mock_bus()
        calls = {"n": 0}

        def recv(timeout: float) -> can.Message | None:
            calls["n"] += 1
            # 最初の 2 回は down 中。以降は復帰して読めるようになる
            if calls["n"] <= 2:
                raise can.CanOperationError("Network is down [Error Code 100]")
            return None

        bus.recv.side_effect = recv
        mgr.add_bus("can0", bus)

        task = asyncio.create_task(mgr._receive_loop("can0"))
        # 再試行のバックオフ (20ms -> 40ms) を跨ぐだけ待つ
        await asyncio.sleep(0.15)

        assert not task.done(), "受信ループが降りている (断絶で死んではならない)"
        assert calls["n"] > 2, "復帰後に呼び直していない"

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    async def test_断絶中はヘルスがDOWNになり復帰でOKへ戻る(self) -> None:
        """降りないだけでは足りない。読めていないことが見えなければ黙殺と同じ。"""
        mgr = CANManager()
        bus = mock_bus()
        state = {"phase": "down"}
        frame = can.Message(arbitration_id=0x201, data=bytes(8), is_extended_id=False)

        def recv(timeout: float) -> can.Message | None:
            if state["phase"] == "down":
                raise can.CanOperationError("Network is down [Error Code 100]")
            return frame

        bus.recv.side_effect = recv
        mgr.add_bus("can0", bus)

        task = asyncio.create_task(mgr._receive_loop("can0"))
        await asyncio.sleep(0.05)
        assert mgr.health().buses[0].state is BusHealth.DOWN
        assert mgr.health().buses[0].rx_down is True

        state["phase"] = "up"
        await asyncio.sleep(0.3)  # 伸びたバックオフを跨いで復帰させる

        assert mgr.health().buses[0].rx_down is False
        assert mgr.health().buses[0].state is BusHealth.OK

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    async def test_タイムアウトは復帰の証拠にならない(self) -> None:
        """`recv` が None を返しても、インタフェースが戻ったとは言えない。

        python-can の socketcan は select がタイムアウトした時点で socket に
        触れずに None を返すので、**down している間も None は返り続ける**。
        これを復帰扱いにすると `rx_down` が数十 ms で勝手に外れ、画面は
        「読めていない」ことを一度も出さないまま平常を映す。

        実際に vcan で down させたまま「30ms で受信が再開しました」と
        誤判定した回帰。
        """
        mgr = CANManager()
        bus = mock_bus()
        calls = {"n": 0}

        def recv(timeout: float) -> can.Message | None:
            calls["n"] += 1
            # 最初の 1 回だけ実エラー。以降は down のままタイムアウトし続ける
            if calls["n"] == 1:
                raise can.CanOperationError("Network is down [Error Code 100]")
            return None

        bus.recv.side_effect = recv
        mgr.add_bus("can0", bus)

        task = asyncio.create_task(mgr._receive_loop("can0"))
        await asyncio.sleep(0.2)

        assert calls["n"] > 2, "タイムアウトを繰り返す状況になっていない"
        assert mgr.health().buses[0].rx_down is True, (
            "タイムアウトを復帰扱いにしている (down 中でも None は返り続ける)"
        )

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    async def test_キャンセルは握り潰さない(self) -> None:
        """``shutdown()`` が畳む唯一の経路。握ると停止できないタスクになる。"""
        mgr = CANManager()
        bus = mock_bus()
        bus.recv.side_effect = can.CanOperationError("Network is down")
        mgr.add_bus("can0", bus)

        task = asyncio.create_task(mgr._receive_loop("can0"))
        await asyncio.sleep(0.03)
        task.cancel()

        # **`await task` を直に書かないこと。** 握り潰されているとそこで永久に
        # 止まり、テストは「落ちる」のではなく「終わらない」形になる。
        # 期限付きで待って、止まらないことを失敗として言い切る
        done, _pending = await asyncio.wait({task}, timeout=1.0)
        assert task in done, "cancel() が効いていない (CancelledError を握り潰している)"

        with pytest.raises(asyncio.CancelledError):
            await task

    async def test_recvが投げたキャンセルも握り潰さない(self) -> None:
        """`bus.recv` の中から来た `CancelledError` も素通しする。

        `CancelledError` は `BaseException` 側にあるので `except Exception` では
        捕まらない —— **つまりここは既定で正しい。** それでも独立した試験を置くのは、
        捕捉を `BaseException` へ広げる変更が入った瞬間に「止められない受信ループ」が
        できるため。しかもその壊れ方は、期限を付けずに待つ試験では「落ちる」ではなく
        「終わらない」形で現れ、原因が読めない。
        """
        mgr = CANManager(run_blocking=direct_runner())
        bus = mock_bus()
        bus.recv.side_effect = asyncio.CancelledError
        mgr.add_bus("can0", bus)

        task = asyncio.create_task(mgr._receive_loop("can0"))
        done, _pending = await asyncio.wait({task}, timeout=1.0)

        assert task in done, "recv 由来のキャンセルを握り潰している (止められない受信ループ)"
        with pytest.raises(asyncio.CancelledError):
            await task


class TestReceiveLoopOnAPollableBus:
    """**実機 (SocketCAN) が通る経路。** fd の可読通知で起きて滞留を出し切る。

    ここを 1 通ずつエグゼキュータへ往復する形にすると、往復のコスト (実測 168us)
    が受信速度の上限を決めてしまう。C620 は 1 台 1kHz なので M3508 2 台だけで
    2000 通/秒あり、追いつかない分はカーネルがソケットバッファ溢れとして捨てる
    (実機で 17%)。**捨てられた窓は M3508 の折り返し推定を狂わせ、累積角に
    360deg = 6.54mm が入る** —— 症状は「動作中に軸が荒れて同期ずれで緊急停止」。

    ``mock_bus`` (MagicMock) は ``fileno()`` が int を返さないのでこの経路に
    入らない。本番の経路を踏むテストは ``ReadableBus`` を使うこと。
    """

    async def _run_until_idle(self, mgr: CANManager, bus_name: str = "can0") -> None:
        """受信ループを起こし、配り終えたところで畳む。"""
        task = asyncio.create_task(mgr._receive_loop(bus_name))
        for _ in range(20):
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    async def test_滞留した複数通が1回の起床で全部配られる(self) -> None:
        calls: list[tuple[Any, tuple[Any, ...]]] = []
        mgr = CANManager(run_blocking=direct_runner(calls))
        motor = GenericDriver("gripper", 0x01)
        bus = ReadableBus()
        mgr.add_bus("can0", bus)
        mgr.add_motor("can0", motor)

        bus.queue(*(generic_feedback(motor, position=float(deg)) for deg in (10, 20, 30)))

        await self._run_until_idle(mgr)

        # 最後の 1 通まで配られている (途中で往復を挟んで取りこぼしていない)
        assert motor.state.position == pytest.approx(30.0)
        assert mgr.last_feedback_at("gripper") is not None
        # **エグゼキュータを 1 度も使っていないこと。** 使うなら 1 通ごとの往復に
        # 戻っており、この経路を用意した意味が無い
        assert calls == []

    async def test_滞留を捌く途中で他のタスクが走る(self) -> None:
        """滞留が深くても制御周期を締め出してはならない。

        1 回の起床で在庫を無制限に捌くと、その間ずっと同期的に走り続けるので
        **位置制御ループ (200Hz) と偏差監視 (50Hz) が滞留を捌き終わるまで
        一切走れない**。`_RX_BATCH_MAX` はその 1 区切りの上限で、区切りごとに
        `_ReadableFd.wait()` が必ずイベントループへ戻ることで成立する。

        「途中で走った」ことは、配り終える前の中間状態を他のタスクが観測できたか
        で見る。最終値しか観測できないなら、そのタスクは締め出されている。
        """
        mgr = CANManager(run_blocking=direct_runner())
        motor = GenericDriver("gripper", 0x01)
        bus = ReadableBus()
        mgr.add_bus("can0", bus)
        mgr.add_motor("can0", motor)

        total = _RX_BATCH_MAX * 3
        bus.queue(*(generic_feedback(motor, position=float(deg)) for deg in range(1, total + 1)))

        seen: list[float] = []

        async def competing_task() -> None:
            while True:
                seen.append(motor.state.position)
                await asyncio.sleep(0)

        rival = asyncio.create_task(competing_task())
        await self._run_until_idle(mgr)
        rival.cancel()

        assert motor.state.position == pytest.approx(float(total)), "滞留を捌き切っていない"
        mid = [pos for pos in seen if 0.0 < pos < float(total)]
        assert mid, "配り終えるまで他のタスクが 1 度も走っていない (制御周期を締め出す)"

    async def test_取り込み中の失敗でも既に引き取った分は捨てない(self) -> None:
        """カーネルのバッファから出したフレームはもうどこにも残っていない。

        ここで捨てると、その窓は M3508 の折り返し推定から永久に失われる
        (=捨てた側が原因の同期ずれを作る)。失敗の報告は次の呼び出しで足りる。
        """
        mgr = CANManager(run_blocking=direct_runner())
        motor = GenericDriver("gripper", 0x01)
        bus = ReadableBus()
        mgr.add_bus("can0", bus)
        mgr.add_motor("can0", motor)

        bus.queue(
            generic_feedback(motor, position=45.0),
            can.CanOperationError("Error receiving: Network is down [Error Code 100]"),
        )

        await self._run_until_idle(mgr)

        assert motor.state.position == pytest.approx(45.0)

    async def test_復帰待ちのあいだは可読の監視を外す(self) -> None:
        """down した socket は「読める」と報告され続ける。

        監視に載せたまま復帰を待つと、**イベントループが毎周期そのコールバックで
        起こされ**、待っているはずの時間が空回りになる。同居している位置制御ループ
        (200Hz) と偏差監視 (50Hz) の周期まで巻き添えにするので、素の ``continue``
        を禁じているのと同じ理由でここも外す。

        ``remove_reader`` は「外すものがあったか」を返すので、実装が既に外して
        いれば False になる。
        """
        mgr = CANManager(run_blocking=direct_runner())
        bus = ReadableBus()
        mgr.add_bus("can0", bus)
        # 何度読んでも失敗し続ける (インタフェースが戻らない状態)
        bus.queue(*(can.CanOperationError("Network is down") for _ in range(20)))

        task = asyncio.create_task(mgr._receive_loop("can0"))
        for _ in range(5):
            await asyncio.sleep(0)  # 1 度失敗してバックオフへ入るまで進める

        loop = asyncio.get_running_loop()
        assert loop.remove_reader(bus.fileno()) is False, (
            "復帰待ちのあいだ可読の監視を載せたままにしている (イベントループが空回りする)"
        )
        assert mgr.health().buses[0].state is BusHealth.DOWN

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    async def test_監視できないバスは従来の経路へ落ちる(self) -> None:
        """``--dry-run`` の virtual バスは ``fileno()`` を持たない。

        ここで例外にすると、机上での配線確認ごと起動しなくなる。
        """
        calls: list[tuple[Any, tuple[Any, ...]]] = []
        mgr = CANManager(run_blocking=direct_runner(calls))
        motor = GenericDriver("gripper", 0x01)
        bus = mock_bus()
        mgr.add_bus("can0", bus)
        mgr.add_motor("can0", motor)
        bus.recv.side_effect = [generic_feedback(motor, position=12.0), asyncio.CancelledError]

        with pytest.raises(asyncio.CancelledError):
            await mgr._receive_loop("can0")

        assert motor.state.position == pytest.approx(12.0)
        # フォールバックはエグゼキュータ経由 (ブロッキング呼び出しをループ上で行わない)
        assert calls, "virtual バスでエグゼキュータを経由していない"
