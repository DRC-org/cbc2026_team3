from __future__ import annotations

import asyncio
import logging
import struct
import time
from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import can
import pytest

from lib.can_manager import _RECV_RETRY_INTERVAL_S, CANManager
from lib.drivers.base import MotorState
from lib.drivers.generic import CommandType, GenericDriver
from lib.drivers.m3508 import M3508Driver
from tests.fake_can import mark_feedback_at


def _make_mock_bus() -> MagicMock:
    bus = MagicMock()
    bus.recv.return_value = None
    return bus


def _direct_runner(
    record: list[tuple[Any, tuple[Any, ...]]] | None = None,
) -> Callable[..., Awaitable[Any]]:
    """ブロッキング呼び出しをその場で実行する ``run_blocking`` (テスト用)。

    エグゼキュータの差し替えを ``patch("asyncio.get_event_loop")`` で行うと、
    「実装がどの API でループを取るか」にテストが固着し、正しい
    ``get_running_loop()`` へ直した瞬間にテストが偽陽性で落ちる。
    差し替え口はコンストラクタ引数として公開されているものだけを使う。
    """

    async def run(func: Callable[..., Any], *args: Any) -> Any:
        if record is not None:
            record.append((func, args))
        return func(*args)

    return run


def _make_mock_motor(name: str, can_id: int) -> MagicMock:
    motor = MagicMock()
    motor.name = name
    motor.can_id = can_id
    motor.matches_feedback.return_value = False
    motor.update_state.return_value = MotorState()
    motor.initialization_steps.return_value = []
    motor.activation_steps.return_value = []
    motor.requires_fresh_feedback_for_activation.return_value = False
    motor.feedback_probe_message.return_value = None
    return motor


class TestCANManager:
    def test_add_bus_and_motor(self) -> None:
        mgr = CANManager()
        bus = _make_mock_bus()
        motor = _make_mock_motor("m1", 1)

        mgr.add_bus("can0", bus)
        mgr.add_motor("can0", motor)

        assert mgr.get_motor("m1") is motor

    def test_get_motor(self) -> None:
        mgr = CANManager()
        bus = _make_mock_bus()
        motor = _make_mock_motor("drive", 2)

        mgr.add_bus("can0", bus)
        mgr.add_motor("can0", motor)

        assert mgr.get_motor("drive") is motor

    def test_get_motor_not_found(self) -> None:
        mgr = CANManager()
        with pytest.raises(KeyError):
            mgr.get_motor("nonexistent")

    async def test_send_to_correct_bus(self) -> None:
        calls: list[tuple[Any, tuple[Any, ...]]] = []
        mgr = CANManager(run_blocking=_direct_runner(calls))
        bus0 = _make_mock_bus()
        bus1 = _make_mock_bus()
        motor = _make_mock_motor("m1", 1)

        mgr.add_bus("can0", bus0)
        mgr.add_bus("can1", bus1)
        mgr.add_motor("can0", motor)

        msg = can.Message(arbitration_id=0x200, data=bytes(8))
        await mgr.send("m1", msg)

        assert calls == [(bus0.send, (msg,))]

    async def test_initialize_motors_sends_steps_with_declared_delays(self) -> None:
        mgr = CANManager()
        bus = _make_mock_bus()
        motor = _make_mock_motor("m1", 1)
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
        mgr.add_bus("can0", _make_mock_bus())

        with patch.object(mgr, "initialize_motors", new_callable=AsyncMock) as initialize_motors:
            await mgr.run()

        initialize_motors.assert_awaited_once_with()
        assert len(mgr._tasks) == 1
        await mgr.shutdown()

    async def test_receive_updates_motor_state(self) -> None:
        mgr = CANManager(run_blocking=_direct_runner())
        bus = _make_mock_bus()
        motor = _make_mock_motor("m1", 1)
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
        bus0 = _make_mock_bus()
        bus1 = _make_mock_bus()

        mgr.add_bus("can0", bus0)
        mgr.add_bus("can1", bus1)

        await mgr.shutdown()

        bus0.shutdown.assert_called_once()
        bus1.shutdown.assert_called_once()

    async def test_shutdown_は死んだ受信タスクの例外で止まらない(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """止める処理が止まってはならない。

        バスが down していると受信ループは ``CanOperationError`` で即死する。
        その例外を ``shutdown()`` が再送出すると、``main()`` の finally が
        そこで折れて 2 台目のロボットのバスが開いたまま残る。
        """
        # 既定のエグゼキュータ経由の runner を使う。同期実行の runner だと
        # 正常な方のバスの受信ループがイベントループへ譲らず回り続けてしまう
        mgr = CANManager()
        bus0 = _make_mock_bus()
        bus0.recv.side_effect = can.CanOperationError("インタフェース断")
        bus1 = _make_mock_bus()
        mgr.add_bus("can0", bus0)
        mgr.add_bus("can1", bus1)

        with caplog.at_level(logging.ERROR, logger="lib.can_manager"):
            await mgr.run()
            # 受信タスクが自力で死ぬまで待つ
            await asyncio.sleep(0.01)
            await mgr.shutdown()

        bus0.shutdown.assert_called_once()
        bus1.shutdown.assert_called_once()

    async def test_shutdown_は1本のバス停止失敗で残りを諦めない(self) -> None:
        mgr = CANManager()
        bus0 = _make_mock_bus()
        bus0.shutdown.side_effect = RuntimeError("デバイスが既に外れている")
        bus1 = _make_mock_bus()
        mgr.add_bus("can0", bus0)
        mgr.add_bus("can1", bus1)

        await mgr.shutdown()

        bus1.shutdown.assert_called_once()


class TestMotorActivation:
    """励磁の有効化は「有効化した瞬間に動かない」ことを保証してからでないと行えない。"""

    def _prepare(self) -> tuple[CANManager, MagicMock]:
        mgr = CANManager()
        mgr.add_bus("can0", _make_mock_bus())
        motor = _make_mock_motor("m1", 1)
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
        mgr.add_bus("can0", _make_mock_bus())
        enable_msg = can.Message(arbitration_id=0x202, data=bytes(8))
        for index, name in enumerate(("m1", "m2", "m3"), start=1):
            motor = _make_mock_motor(name, index)
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
        mgr.add_bus("can0", _make_mock_bus())
        msg = can.Message(arbitration_id=0x202, data=bytes(8))
        for index, name in enumerate(("m1", "m2"), start=1):
            motor = _make_mock_motor(name, index)
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

    @staticmethod
    def _feedback_msg(device_id: int, position_dg: int) -> can.Message:
        # Byte0=状態フラグ / Byte1-2=位置 (仕様書 §3.2)
        data = bytearray([0x00])
        data.extend(struct.pack("<h", position_dg))
        return can.Message(
            arbitration_id=GenericDriver.build_can_id(CommandType.FEEDBACK, device_id),
            data=bytes(data),
            is_extended_id=False,
        )

    @staticmethod
    def _m3508_feedback(can_id: int, angle_raw: int) -> can.Message:
        return can.Message(
            arbitration_id=0x200 + can_id,
            data=struct.pack(">hhhB", angle_raw, 0, 0, 30) + bytes(1),
            is_extended_id=False,
        )

    async def _run_loop(self, mgr: CANManager) -> None:
        with pytest.raises(asyncio.CancelledError):
            await mgr._receive_loop("can0")

    @pytest.mark.parametrize("reserved_command_type", [0b101, 0b110, 0b111])
    async def test_receive_loop_survives_reserved_command_type(
        self, reserved_command_type: int
    ) -> None:
        mgr = CANManager(run_blocking=_direct_runner())
        bus = _make_mock_bus()
        motor = GenericDriver("gripper", 0x01)
        mgr.add_bus("can0", bus)
        mgr.add_motor("can0", motor)

        bogus = can.Message(
            arbitration_id=(reserved_command_type << 8) | 0x01,
            data=bytes(8),
            is_extended_id=False,
        )
        self._drain_recv(bus, [bogus, self._feedback_msg(0x01, 900)])

        await self._run_loop(mgr)

        # 予約種別のフレームの後でも、続くフィードバックが取り込めていること
        assert motor.state.position == pytest.approx(90.0)

    async def test_receive_loop_survives_extended_frame(self) -> None:
        mgr = CANManager(run_blocking=_direct_runner())
        bus = _make_mock_bus()
        motor = GenericDriver("gripper", 0x01)
        mgr.add_bus("can0", bus)
        mgr.add_motor("can0", motor)

        alien = can.Message(arbitration_id=0x12345678, data=bytes(8), is_extended_id=True)
        self._drain_recv(bus, [alien, self._feedback_msg(0x01, 450)])

        await self._run_loop(mgr)

        assert motor.state.position == pytest.approx(45.0)

    async def test_receive_loop_survives_short_m3508_frame(self) -> None:
        """M3508 は DLC を検査せずに struct.unpack する。短いフレームは実際に届く。

        M3508Driver.matches_feedback は arbitration_id しか見ないため、
        バス上の別機器が 0x201〜0x204 を 8 バイト未満で流すだけで decode が落ちる。
        """
        mgr = CANManager(run_blocking=_direct_runner())
        bus = _make_mock_bus()
        hit = M3508Driver("y_axis_r", 1)
        other = M3508Driver("y_axis_l", 2)
        mgr.add_bus("can0", bus)
        mgr.add_motor("can0", hit)
        mgr.add_motor("can0", other)

        short = can.Message(arbitration_id=0x201, data=bytes(4), is_extended_id=False)
        self._drain_recv(bus, [short, self._m3508_feedback(2, angle_raw=2048)])

        await self._run_loop(mgr)

        # 巻き添えを受けずに、後続の正常フレームが取り込めていること
        assert other.state.position == pytest.approx(90.0, abs=0.1)
        assert mgr.last_feedback_at("y_axis_l") is not None
        # デコードできなかった以上、当該モータは「受信した」ことにしてはならない
        assert mgr.last_feedback_at("y_axis_r") is None
        assert mgr._rx_error_count["can0"] == 1

    async def test_receive_loop_isolates_failing_matcher_to_one_motor(self) -> None:
        """matches_feedback が投げるドライバが、同じバスの他モータ宛を巻き添えにしない。"""
        mgr = CANManager(run_blocking=_direct_runner())
        bus = _make_mock_bus()
        broken = _make_mock_motor("broken", 0x02)
        broken.matches_feedback.side_effect = ValueError("解析できない ID")
        healthy = GenericDriver("gripper", 0x01)
        mgr.add_bus("can0", bus)
        mgr.add_motor("can0", broken)
        mgr.add_motor("can0", healthy)

        self._drain_recv(bus, [self._feedback_msg(0x01, 900)])

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
        mgr = CANManager(run_blocking=_direct_runner())
        bus = _make_mock_bus()
        motor = GenericDriver("gripper", 0x01)
        mgr.add_bus("can0", bus)
        mgr.add_motor("can0", motor)

        queue: list[can.Message | Exception] = [
            can.CanOperationError("Error receiving: Network is down [Error Code 100]"),
            self._feedback_msg(0x01, 900),
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
        """
        mgr = CANManager(run_blocking=_direct_runner())
        bus = _make_mock_bus()
        mgr.add_bus("can0", bus)
        bus.recv.side_effect = can.CanOperationError("Network is down")

        window_s = _RECV_RETRY_INTERVAL_S * 3
        task = asyncio.create_task(mgr._receive_loop("can0"))
        await asyncio.sleep(window_s)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # 待たずに回すと、この窓のあいだに数千回の recv が走る
        spin_free_limit = int(window_s / _RECV_RETRY_INTERVAL_S) + 2
        assert bus.recv.call_count <= spin_free_limit, (
            f"失敗のたびに待たずに再試行している (recv 呼び出し {bus.recv.call_count} 回)"
        )
        assert bus.recv.call_count >= 1, "1 度も再試行していない"

    async def test_receive_loop_propagates_cancelled_error(self) -> None:
        """CancelledError は shutdown の停止経路。握り潰すと止まらない受信ループが残る。"""
        mgr = CANManager(run_blocking=_direct_runner())
        bus = _make_mock_bus()

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
        self._drain_recv(bus, [self._feedback_msg(0x01, 900), self._feedback_msg(0x01, 450)])

        await self._run_loop(mgr)

        # 1 通目で抜けている (2 通目を読みに行っていない) こと
        assert bus.recv.call_count == 1
        # 停止要求は受信エラーではない
        assert mgr._rx_error_count["can0"] == 0

    async def test_receive_failure_is_logged_without_killing_the_loop(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """受信 API 自体の失敗は、痕跡を残したうえで**再試行する**。

        2 つを同時に満たす必要がある:

        - 記録する: ``_tasks`` は誰も await しないので、ここで残さないと
          受信の異常がどこにも現れない
        - **降りない**: 降りると送信側だけが次の周期で自動復帰し、受信は二度と
          戻らない。症状は「指令は効くのにフィードバックだけ永久に無い」で、
          機体は動くのに全モータが STALE のまま試合を終える

        かつては後者が満たされておらず、``ip link`` の再設定が入っただけで
        受信ループが死んだ (実機で発生)。
        """
        mgr = CANManager(run_blocking=_direct_runner())
        bus = _make_mock_bus()
        bus.recv.side_effect = can.CanOperationError("インタフェース断")
        mgr.add_bus("can0", bus)
        mgr.add_motor("can0", GenericDriver("gripper", 0x01))

        with caplog.at_level(logging.ERROR, logger="lib.can_manager"):
            task = asyncio.create_task(mgr._receive_loop("can0"))
            await asyncio.sleep(_RECV_RETRY_INTERVAL_S * 2)
            still_running = not task.done()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert still_running, "受信 API の失敗でループが降りている"
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert errors, "受信の失敗が記録されていない"
        assert errors[0].exc_info is not None

    async def test_receive_error_logs_are_throttled(self, caplog: pytest.LogCaptureFixture) -> None:
        """不正フレームが連続しても、1 通ごとにログを出すと他のログが読めなくなる。"""
        mgr = CANManager(run_blocking=_direct_runner())
        bus = _make_mock_bus()
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
        mgr.add_bus("can_generic", _make_mock_bus())
        mgr.add_motor("can_generic", _make_mock_motor("gripper", 0x01))

        with pytest.raises(ValueError) as excinfo:
            mgr.add_motor("can_generic", _make_mock_motor("gripper", 0x02))

        assert "gripper" in str(excinfo.value)

    def test_duplicate_motor_name_across_buses_is_rejected(self) -> None:
        """名前は _motors の唯一のキーなので、別バスでも後勝ちで上書きされてしまう。"""
        mgr = CANManager()
        mgr.add_bus("can_generic", _make_mock_bus())
        mgr.add_bus("can_edulite", _make_mock_bus())
        mgr.add_motor("can_generic", _make_mock_motor("gripper", 0x01))

        with pytest.raises(ValueError):
            mgr.add_motor("can_edulite", _make_mock_motor("gripper", 0x01))

    def test_duplicate_can_id_on_same_bus_is_rejected(self) -> None:
        mgr = CANManager()
        mgr.add_bus("can_generic", _make_mock_bus())
        mgr.add_motor("can_generic", _make_mock_motor("gripper", 0x01))

        with pytest.raises(ValueError) as excinfo:
            mgr.add_motor("can_generic", _make_mock_motor("wall", 0x01))

        message = str(excinfo.value)
        # どのバスの・どの CAN ID が・どのモータと衝突したかが分からないと現物を追えない
        assert "can_generic" in message
        assert "0x01" in message
        assert "gripper" in message
        assert "wall" in message

    def test_same_can_id_on_different_bus_is_allowed(self) -> None:
        """バスが違えばフレームは混ざらない。ここまで弾くと現実の配線が組めない。"""
        mgr = CANManager()
        mgr.add_bus("can_generic", _make_mock_bus())
        mgr.add_bus("can_edulite", _make_mock_bus())
        mgr.add_motor("can_generic", _make_mock_motor("gripper", 0x01))
        mgr.add_motor("can_edulite", _make_mock_motor("rotate_l", 0x01))

        assert mgr.get_motor("gripper").can_id == mgr.get_motor("rotate_l").can_id

    def test_rejected_motor_is_not_registered(self) -> None:
        """弾いた後に _bus_motors 側だけ残ると、受信ループが孤児へフレームを配る。"""
        mgr = CANManager()
        mgr.add_bus("can_generic", _make_mock_bus())
        first = _make_mock_motor("gripper", 0x01)
        mgr.add_motor("can_generic", first)

        with pytest.raises(ValueError):
            mgr.add_motor("can_generic", _make_mock_motor("wall", 0x01))

        assert mgr._bus_motors["can_generic"] == [first]
        assert set(mgr._motors) == {"gripper"}


class TestReadOnlyViews:
    """構成の読み取り口。サーバー・動作確認がここを通れば private を触らずに済む。"""

    def _mgr(self) -> CANManager:
        mgr = CANManager()
        mgr.add_bus("can_m3508", _make_mock_bus(), channel="vcan0")
        mgr.add_bus("can_generic", _make_mock_bus(), channel="vcan1")
        mgr.add_motor("can_m3508", _make_mock_motor("y_axis_r", 0x01))
        mgr.add_motor("can_m3508", _make_mock_motor("y_axis_l", 0x02))
        mgr.add_motor("can_generic", _make_mock_motor("gripper", 0x01))
        return mgr

    def test_motors_は宣言順を保つ(self) -> None:
        # 動作確認は config の宣言順に 1 台ずつ動かす。順序が崩れると
        # 指差喚呼の読み上げ順と画面の進捗が食い違う
        mgr = self._mgr()
        assert list(mgr.motors) == ["y_axis_r", "y_axis_l", "gripper"]
        assert mgr.motors["gripper"] is mgr.get_motor("gripper")

    def test_motors_は書き換えられない(self) -> None:
        mgr = self._mgr()
        with pytest.raises(TypeError):
            mgr.motors["gripper"] = _make_mock_motor("gripper", 0x09)  # type: ignore[index]

    def test_motors_は登録を追従する(self) -> None:
        mgr = self._mgr()
        view = mgr.motors
        mgr.add_motor("can_generic", _make_mock_motor("wall", 0x02))
        assert "wall" in view

    def test_bus_names_で送信先バスを列挙できる(self) -> None:
        mgr = self._mgr()
        assert mgr.bus_names == ("can_m3508", "can_generic")

    def test_bus_of_でモータの所属バスを引ける(self) -> None:
        mgr = self._mgr()
        assert mgr.bus_of("y_axis_l") == "can_m3508"
        assert mgr.bus_of("gripper") == "can_generic"

    def test_bus_of_は未登録モータで_None(self) -> None:
        # 未登録を KeyError にすると、ヘルス表示のためだけに呼ぶ側が必ず握り潰す羽目になる
        assert self._mgr().bus_of("unknown") is None
