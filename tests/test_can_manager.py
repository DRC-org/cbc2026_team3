from __future__ import annotations

import asyncio
import logging
import struct
import time
from collections.abc import Sequence
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import can
import pytest

from lib.can_manager import _RECV_RETRY_MIN_S, _RX_BATCH_MAX, CANManager
from lib.drivers.base import ControlMode, MotorState
from lib.drivers.dm3520 import Dm3520Driver
from lib.drivers.edulite05 import Edulite05Driver
from lib.drivers.generic import GenericDriver
from lib.drivers.m3508 import M3508Driver
from lib.health import BusHealth
from tests.fake_can import (
    ReadableBus,
    deliver_frame,
    direct_runner,
    mark_feedback_at,
    mock_bus,
    mock_driver,
)
from tests.feedback_frames import (
    dm3520_config_response,
    dm3520_feedback,
    edulite_feedback,
    feed_dm3520,
    feed_edulite,
    generic_feedback,
    m3508_feedback,
)


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
        mgr = CANManager()
        bus0 = mock_bus()
        bus0.recv.side_effect = can.CanOperationError("インタフェース断")
        bus1 = mock_bus()
        mgr.add_bus("can0", bus0)
        mgr.add_bus("can1", bus1)

        await mgr.run()
        await asyncio.sleep(0.03)
        await mgr.shutdown()

        bus0.shutdown.assert_called_once()
        bus1.shutdown.assert_called_once()

    async def test_shutdown_は既に死んでいる受信タスクの例外で止まらない(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:

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


class TestCaptureOriginViaSetZero:
    """`SET_ZERO` で原点を切り直す経路。**DM3520 専用である。**

    EDULITE 05 は原点を PC 側で持つので、この経路を 1 度も通らない
    (`TestCaptureOriginInPlace`)。
    """

    def _prepare(self) -> tuple[CANManager, list[tuple[str, can.Message]]]:
        sent: list[tuple[str, can.Message]] = []
        mgr = CANManager(run_blocking=direct_runner())
        mgr.add_bus("can_dm3520", mock_bus())
        for name, can_id, master_id in (
            ("sub_y_axis_m", 0x01, 0x11),
            ("sub_lift_m", 0x02, 0x12),
        ):
            mgr.add_motor("can_dm3520", Dm3520Driver(name, can_id=can_id, master_id=master_id))

        registers = {
            Dm3520Driver.REG_P_MAX: "p_max",
            Dm3520Driver.REG_V_MAX: "v_max",
            Dm3520Driver.REG_T_MAX: "t_max",
        }

        async def _send(motor_name: str, msg: can.Message) -> None:
            sent.append((motor_name, msg))
            driver = mgr.motors[motor_name]
            data = bytes(msg.data)
            if msg.arbitration_id == Dm3520Driver.CONFIG_FRAME_ID and data[3] in registers:
                # 読み返せないモータは励磁を拒否される (`activation_block_reason`)
                deliver_frame(
                    mgr,
                    "can_dm3520",
                    dm3520_config_response(driver, data[3], getattr(driver, registers[data[3]])),
                )
                return
            mark_feedback_at(mgr, motor_name, time.time())

        mgr.send = _send  # type: ignore[method-assign]
        return mgr, sent

    @staticmethod
    def _specials(sent: list[tuple[str, can.Message]], name: str) -> list[int]:
        # 特殊コマンドは 3 つとも同じ CAN ID なので、見分けが付くのは末尾バイトだけ
        return [
            msg.data[7]
            for n, msg in sent
            if n == name and msg.arbitration_id < Dm3520Driver.POSITION_CMD_BASE
        ]

    async def test_無励磁にしてから原点を切り直す(self) -> None:
        mgr, sent = self._prepare()

        await mgr.capture_origin_via_set_zero(["sub_y_axis_m", "sub_lift_m"])

        for name in ("sub_y_axis_m", "sub_lift_m"):
            order = self._specials(sent, name)
            zero = order.index(Dm3520Driver.SPECIAL_SET_ZERO)
            assert Dm3520Driver.SPECIAL_DISABLE in order[:zero]

    async def test_切り直した瞬間に零点確定済みになる(self) -> None:
        """原点はモータ内部に書かれるので、確定したことをドライバへ伝えられるのはここだけ。"""
        mgr, _sent = self._prepare()
        assert all(not mgr.motors[n].origin_confirmed() for n in ("sub_y_axis_m", "sub_lift_m"))

        await mgr.capture_origin_via_set_zero(["sub_y_axis_m", "sub_lift_m"])

        assert all(mgr.motors[n].origin_confirmed() for n in ("sub_y_axis_m", "sub_lift_m"))

    async def test_全員を無励磁にしてから全員を切り直す(self) -> None:
        mgr, sent = self._prepare()

        await mgr.capture_origin_via_set_zero(["sub_y_axis_m", "sub_lift_m"])

        specials = [
            msg.data[7] for _n, msg in sent if msg.arbitration_id < Dm3520Driver.POSITION_CMD_BASE
        ]
        first_zero = specials.index(Dm3520Driver.SPECIAL_SET_ZERO)
        assert specials[:first_zero].count(Dm3520Driver.SPECIAL_DISABLE) == 2

    async def test_新原点を保持目標にして再励磁する(self) -> None:
        mgr, sent = self._prepare()
        for name in ("sub_y_axis_m", "sub_lift_m"):
            feed_dm3520(mgr.motors[name], position=1.5)

        await mgr.capture_origin_via_set_zero(["sub_y_axis_m", "sub_lift_m"])

        for name in ("sub_y_axis_m", "sub_lift_m"):
            order = self._specials(sent, name)
            zero = order.index(Dm3520Driver.SPECIAL_SET_ZERO)
            enable = order.index(Dm3520Driver.SPECIAL_ENABLE, zero)
            targets = [
                msg
                for n, msg in sent
                if n == name and msg.arbitration_id >= Dm3520Driver.POSITION_CMD_BASE
            ]
            p_des = struct.unpack("<f", targets[-1].data[:4])[0]
            assert p_des == pytest.approx(0.0), "旧原点の実測角 1.5rad を書いてはならない"
            assert enable > zero

    async def test_付け替え中に緊急停止が入ったら励磁しない(self) -> None:
        """**緊急停止を「押した瞬間の状態」に依存させない。**

        付け替えの窓 (disable 0.05s + `SET_ZERO` 0.2s + 鮮度待ち + 0.15s ≒ 0.5 秒)
        で停止が入ると、`_send_e_stop_frames` の disable の**後**に enable が届き、
        **停止中に励磁されたまま残る**。ログにもヘルスにも出ない。
        """
        mgr, sent = self._prepare()

        with pytest.raises(RuntimeError, match="再励磁"):
            await mgr.capture_origin_via_set_zero(
                ["sub_y_axis_m", "sub_lift_m"], should_abort=lambda: True
            )

        for name in ("sub_y_axis_m", "sub_lift_m"):
            order = self._specials(sent, name)
            assert Dm3520Driver.SPECIAL_ENABLE not in order, "停止中に励磁してはならない"
            # **中断してよいのは励磁だけ。** 無励磁化と付け替えは機体を動かさない
            # 指令で、途中で降りると「片方だけ付け替えた」状態が残る
            assert Dm3520Driver.SPECIAL_DISABLE in order
            assert Dm3520Driver.SPECIAL_SET_ZERO in order

    async def test_再励磁できなければ降りる(self) -> None:
        mgr = CANManager(run_blocking=direct_runner())
        mgr.add_bus("can_dm3520", mock_bus())
        mgr.add_motor("can_dm3520", Dm3520Driver("sub_y_axis_m", can_id=0x01, master_id=0x11))

        with (
            patch.object(mgr, "send", new_callable=AsyncMock),
            pytest.raises(RuntimeError, match="再励磁"),
        ):
            await mgr.capture_origin_via_set_zero(["sub_y_axis_m"])

    async def test_手段を持たないモータは拒否する(self) -> None:
        mgr = CANManager(run_blocking=direct_runner())
        mgr.add_bus("can_generic", mock_bus())
        mgr.add_motor("can_generic", GenericDriver("servo", can_id=0x41))

        with pytest.raises(ValueError, match="原点を切り直せません"):
            await mgr.capture_origin_via_set_zero(["servo"])


class TestCaptureOriginInPlace:
    """原点を PC 側のオフセットで控える経路 (EDULITE 05)。CAN へは 1 通も出ない。"""

    _NAMES = ("rotate_r", "rotate_l")

    def _prepare(
        self, *, received: Sequence[str] = _NAMES
    ) -> tuple[CANManager, list[tuple[str, can.Message]]]:
        sent: list[tuple[str, can.Message]] = []
        mgr = CANManager(run_blocking=direct_runner())
        mgr.add_bus("can_edulite", mock_bus())
        for name, can_id in (("rotate_r", 0x11), ("rotate_l", 0x12)):
            mgr.add_motor("can_edulite", Edulite05Driver(name, can_id=can_id))
        for name in received:
            deliver_frame(mgr, "can_edulite", edulite_feedback(mgr.motors[name], position=1.5))

        async def _send(motor_name: str, msg: can.Message) -> None:
            sent.append((motor_name, msg))

        mgr.send = _send  # type: ignore[method-assign]
        return mgr, sent

    async def test_全員の原点を控える(self) -> None:
        mgr, _sent = self._prepare()

        await mgr.capture_origin_in_place(list(self._NAMES))

        for name in self._NAMES:
            assert mgr.motors[name].feedback_position() == pytest.approx(0.0, abs=1e-9)

    async def test_CANへは1通も出さない(self) -> None:
        """無励磁化も再励磁も要らない。送れば機構が動く窓がそのぶん開く。"""
        mgr, sent = self._prepare()

        await mgr.capture_origin_in_place(list(self._NAMES))

        assert sent == []

    async def test_全員を同じ処理の中で控える(self) -> None:
        """**別々の時刻に控えると、その間に動いたぶんが消えないオフセットになる。**

        `rotate` は逆回転ペアなので、残ったオフセットはそのまま偏差として立ち、
        正常な動作でも即座に `SyncMonitor` が全体緊急停止を掛ける。
        """
        mgr, _sent = self._prepare()
        captured: list[str] = []

        async def _move_after_first_capture() -> None:
            # 1 台目を控えた後にイベントループが回るなら、そこで姿勢が変わりうる
            while True:
                if captured:
                    for name in self._NAMES:
                        deliver_frame(
                            mgr, "can_edulite", edulite_feedback(mgr.motors[name], position=3.0)
                        )
                await asyncio.sleep(0)

        for name in self._NAMES:
            driver = mgr.motors[name]
            original = driver.capture_origin_here

            def _spy(name: str = name, original: Any = original) -> None:
                captured.append(name)
                original()

            driver.capture_origin_here = _spy  # type: ignore[method-assign]

        mover = asyncio.create_task(_move_after_first_capture())
        try:
            await mgr.capture_origin_in_place(list(self._NAMES))
        finally:
            mover.cancel()

        assert captured == list(self._NAMES)
        offsets = [mgr.motors[name].origin_offset for name in self._NAMES]
        assert offsets[0] == pytest.approx(offsets[1])

    async def test_1台でも未受信なら1台も控えない(self) -> None:
        """**未受信の 0.0 を現在位置と信じてはならない。**

        控えた側だけが新しい座標系に移り、ペアの偏差がそのまま全体緊急停止になる。
        """
        mgr, _sent = self._prepare(received=["rotate_r"])

        with pytest.raises(RuntimeError, match="rotate_l"):
            await mgr.capture_origin_in_place(list(self._NAMES))

        for name in self._NAMES:
            assert mgr.motors[name].origin_offset == 0.0

    async def test_フィードバックが古ければ控えない(self) -> None:
        mgr, _sent = self._prepare()
        mark_feedback_at(mgr, "rotate_l", time.time() - 10.0)

        with pytest.raises(RuntimeError, match="rotate_l"):
            await mgr.capture_origin_in_place(list(self._NAMES))

        assert mgr.motors["rotate_r"].origin_offset == 0.0

    async def test_緊急停止が入っていたら1台も控えない(self) -> None:
        """探索が完遂していない姿勢を原点にすると、黙って成功した誤った原点が残る。"""
        mgr, _sent = self._prepare()

        with pytest.raises(RuntimeError, match="緊急停止"):
            await mgr.capture_origin_in_place(list(self._NAMES), should_abort=lambda: True)

        for name in self._NAMES:
            assert mgr.motors[name].origin_offset == 0.0

    async def test_手段を持たないモータは拒否する(self) -> None:
        mgr = CANManager(run_blocking=direct_runner())
        mgr.add_bus("can_generic", mock_bus())
        mgr.add_motor("can_generic", GenericDriver("servo", can_id=0x41))

        with pytest.raises(ValueError, match="原点"):
            await mgr.capture_origin_in_place(["servo"])


class TestEduliteReenergize:
    """モックではなく EDULITE 05 の実体で、再初期化のゲートを 1 枚で確かめる。

    **`_is_known_energized` は `is_energized()` の三値をドライバへ聞く。** モック版
    だけでは、実機のドライバがその三値を報告しなくなっても誰も気付けない。
    """

    def _prepare(
        self, *, set_zero_on_start: bool = False
    ) -> tuple[CANManager, Edulite05Driver, list[can.Message]]:
        sent: list[can.Message] = []
        mgr = CANManager(run_blocking=direct_runner())
        mgr.add_bus("can_edulite", mock_bus())
        driver = Edulite05Driver(
            "rotate_r", can_id=0x11, position_kp=30.0, set_zero_on_start=set_zero_on_start
        )
        mgr.add_motor("can_edulite", driver)

        async def _send(motor_name: str, msg: can.Message) -> None:
            sent.append(msg)
            mark_feedback_at(mgr, motor_name, time.time())

        mgr.send = _send  # type: ignore[method-assign]
        return mgr, driver, sent

    async def test_無励磁なら電源断で失われた設定を書き直す(self) -> None:
        mgr, driver, sent = self._prepare()
        feed_edulite(driver, mode_state=0)

        await mgr.activate_motors()

        params = [
            struct.unpack_from("<H", msg.data)[0]
            for msg in sent
            if Edulite05Driver.parse_can_id(msg.arbitration_id)[0]
            == Edulite05Driver.COMM_TYPE_WRITE_PARAM
        ]
        assert params[:4] == [
            Edulite05Driver.PARAM_RUN_MODE,
            Edulite05Driver.PARAM_LIMIT_SPD,
            Edulite05Driver.PARAM_LIMIT_CUR,
            Edulite05Driver.PARAM_LOC_KP,
        ]

    async def test_励磁中なら再初期化を送らない(self) -> None:
        """再初期化は `disable` で始まる。直結ペアの健全な相方は保持トルクを失う。"""
        mgr, driver, sent = self._prepare()
        feed_edulite(driver, mode_state=2)

        await mgr.activate_motors(feedback_timeout_s=0.05)

        assert sent == []

    async def test_起動時に暫定原点を控える(self) -> None:
        """左右の機械ゼロ差 (実機 175.879deg) を消さないと 1 ステップも動かせない。"""
        mgr, driver, _sent = self._prepare(set_zero_on_start=True)
        feed_edulite(driver, position=1.0, mode_state=0)

        await mgr.initialize_motors()

        assert driver.origin_offset == pytest.approx(driver.state.position)
        assert driver.feedback_position() == pytest.approx(0.0, abs=1e-9)

    async def test_再励磁を繰り返しても原点が動かない(self) -> None:
        """**物理緊急停止からの復帰は再励磁を通る。**

        そのたびに暫定原点を控え直すと、復帰時点の姿勢が新しい原点になる。
        `rotate` は逆回転ペアなので、左右で別々の姿勢を控えたぶんが偏差として残り、
        解除するたびに `SyncMonitor` が全体緊急停止を掛け直して復帰できない。
        """
        mgr, driver, _sent = self._prepare(set_zero_on_start=True)
        feed_edulite(driver, position=1.0, mode_state=0)

        await mgr.activate_motors()
        offset = driver.origin_offset

        # 復帰までに機構が動いた (あるいは電源断で報告値が畳まれた) ことにする
        feed_edulite(driver, position=2.0, mode_state=0)
        await mgr.activate_motors()

        assert driver.origin_offset == pytest.approx(offset)


class TestMotorActivation:
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

    async def test_再励磁は電源断で失われた設定を送り直す(self) -> None:
        """**物理非常停止は DM3520 の電源を数秒落とす。**

        復帰した個体は CTRL_MODE も固定小数点レンジも出荷値へ戻っているのに、
        再励磁経路は長いあいだ `activate_motors` しか呼んでいなかった。戻った
        モードで励磁すると「励磁を名乗るのにトルクが出ない」か、12.5 で送られた
        フィードバックを 1000 で復号した **80 倍の保持目標**が書かれる。
        """
        mgr, motor = self._prepare()
        reinit = can.Message(arbitration_id=0x7FF, data=bytes(8))
        enable = can.Message(arbitration_id=0x201, data=bytes(8))
        motor.reinitialization_steps.return_value = [(reinit, 0.0)]
        motor.activation_steps.return_value = [(enable, 0.0)]

        with patch.object(mgr, "send", new_callable=AsyncMock) as send:
            await mgr.activate_motors()

        assert [call.args[1] for call in send.await_args_list] == [reinit, enable]

    async def test_起動経路は再初期化を送らない(self) -> None:
        """起動は `initialization_steps()` を丸ごと送った直後なので要らない。

        二重に送ると、`set_zero_on_start` の軸で `SET_ZERO` が 2 回飛ぶ。
        """
        mgr, motor = self._prepare()

        with patch.object(mgr, "send", new_callable=AsyncMock):
            await mgr.initialize_motors()

        motor.reinitialization_steps.assert_not_called()

    async def test_励磁中のモータへは再初期化を送らない(self) -> None:
        """**再初期化は `disable` で始まる。** 直結ペアの健全な相方へ届くと、
        その場で保持トルクを失う (`sub_lift` は自重で落ちる)。

        再励磁の `only` には励磁中のモータが混ざる —— 片側だけ落ちたペアでは
        健全な相方も対象に含めるため。判断は `_may_probe_for_feedback` と同じ
        `is_energized()` の三値で、電源が落ちた個体は復帰後 `False` を報告する
        ので、本当に要るモータには届く。
        """
        mgr, motor = self._prepare()
        motor.is_energized.return_value = True
        reinit = can.Message(arbitration_id=0x7FF, data=bytes(8))
        enable = can.Message(arbitration_id=0x201, data=bytes(8))
        motor.reinitialization_steps.return_value = [(reinit, 0.0)]
        motor.activation_steps.return_value = [(enable, 0.0)]

        with patch.object(mgr, "send", new_callable=AsyncMock) as send:
            await mgr.activate_motors()

        motor.reinitialization_steps.assert_not_called()
        # 励磁そのものは通す (無励磁の相方を戻すための呼び出しに巻き込まれただけで、
        # このモータの目標を書き直すこと自体は害が無い)
        assert [call.args[1] for call in send.await_args_list] == [enable]

    async def test_暫定原点は鮮度を確かめてから控える(self) -> None:
        """**未受信の 0.0 を現在位置と信じて原点にしてはならない。**

        先に控えると、起動直後の原点が「まだ 1 通も届いていない」姿勢になり、
        以後の指令が全部そのぶんずれた場所を指す。
        """
        mgr, motor = self._prepare()
        motor.requires_fresh_feedback_for_activation.return_value = True
        motor.feedback_probe_message.return_value = can.Message(arbitration_id=0x203, data=bytes(8))
        order: list[str] = []

        async def _send(motor_name: str, _msg: can.Message) -> None:
            order.append("問い合わせ")
            mark_feedback_at(mgr, motor_name, time.time())

        def _establish() -> bool:
            order.append("暫定原点")
            return False

        motor.establish_provisional_origin.side_effect = _establish
        mgr.send = _send  # type: ignore[method-assign]

        await mgr.activate_motor("m1")

        assert order == ["問い合わせ", "暫定原点"]

    async def test_activation_reads_position_after_fresh_feedback_arrives(self) -> None:
        mgr, motor = self._prepare()
        motor.requires_fresh_feedback_for_activation.return_value = True
        motor.feedback_probe_message.return_value = can.Message(arbitration_id=0x203, data=bytes(8))
        enable_msg = can.Message(arbitration_id=0x202, data=bytes(8))

        mark_feedback_at(mgr, "m1", time.time())

        seen_rx_at: list[float | None] = []

        def record_activation(*, after_set_zero: bool = False) -> list[tuple[can.Message, float]]:
            seen_rx_at.append(mgr.last_feedback_at("m1"))
            return [(enable_msg, 0.0)]

        motor.activation_steps.side_effect = record_activation

        async def fake_send(name: str, msg: can.Message) -> None:
            mark_feedback_at(mgr, name, time.time())

        with patch.object(mgr, "send", new_callable=AsyncMock, side_effect=fake_send):
            activated = await mgr.activate_motor("m1", feedback_timeout_s=0.5)

        assert activated is True
        assert seen_rx_at and seen_rx_at[0] is not None
        assert seen_rx_at[0] > (mgr.last_feedback_at("m1") or 0.0) - 1.0

    async def test_設定は読めるまで問い合わせ直す(self) -> None:
        """**取りこぼしは再試行で解く。** ゲートの既定を「通す」にして解くと、
        応答 1 通で `activation_block_reason()` が守る経路が丸ごと復活する。"""
        mgr, motor = self._prepare()
        read = can.Message(arbitration_id=0x7FF, data=bytes(8))
        enable_msg = can.Message(arbitration_id=0x202, data=bytes(8))
        pending = [read]
        motor.configuration_probe_messages.side_effect = lambda: list(pending)
        motor.activation_steps.return_value = [(enable_msg, 0.0)]
        attempts = 0

        async def fake_send(name: str, msg: can.Message) -> None:
            nonlocal attempts
            if msg is read:
                attempts += 1
                if attempts >= 3:  # 2 通落ちてから応答が届いた状況
                    pending.clear()

        with patch.object(mgr, "send", new_callable=AsyncMock, side_effect=fake_send) as send:
            # 締切は別のテストが見る。ここで実時間に頼ると負荷でフレークする
            activated = await mgr.activate_motor("m1", feedback_timeout_s=60.0)

        assert activated is True
        assert attempts == 3
        assert send.await_args_list[-1].args[1] is enable_msg

    async def test_設定を読めないまま締切を過ぎたらドライバの判断に従う(self) -> None:
        """**再試行は無限ではない。** 締切を過ぎたら判断はドライバへ戻す ——
        ここで「読めなかったから通す」を書くと、緩む場所がもう 1 つできる。"""
        mgr, motor = self._prepare()
        motor.configuration_probe_messages.return_value = [
            can.Message(arbitration_id=0x7FF, data=bytes(8))
        ]
        motor.activation_block_reason.return_value = "p_max が未確認です"
        motor.activation_steps.return_value = [
            (can.Message(arbitration_id=0x202, data=bytes(8)), 0.0)
        ]

        with patch.object(mgr, "send", new_callable=AsyncMock):
            activated = await mgr.activate_motor("m1", feedback_timeout_s=0.05)

        assert activated is False
        motor.activation_steps.assert_not_called()

    async def test_確認すべき設定が無ければ読み返しを送らない(self) -> None:
        """大半のドライバ (M3508 / 自作モタドラ) は確認すべき設定を持たない。"""
        mgr, motor = self._prepare()
        enable_msg = can.Message(arbitration_id=0x202, data=bytes(8))
        motor.activation_steps.return_value = [(enable_msg, 0.0)]

        with patch.object(mgr, "send", new_callable=AsyncMock) as send:
            activated = await mgr.activate_motor("m1")

        assert activated is True
        assert [call.args[1] for call in send.await_args_list] == [enable_msg]

    async def test_構成が食い違うモータは励磁しない(self) -> None:
        """**「待てば解ける」と「待っても解けない」を分ける。**

        固定小数点レンジの食い違いは待っても解けないので、鮮度待ちのタイムアウトへ
        紛れ込ませてはならない (原因が「通信が遅い」に見えてしまう)。実機では
        p_max の食い違いで位置が 80 倍に読め、その値が保持目標として書かれて機構が
        リミットスイッチを踏み越えた。
        """
        mgr, motor = self._prepare()
        motor.activation_block_reason.return_value = "p_max が食い違っています"
        motor.activation_steps.return_value = [
            (can.Message(arbitration_id=0x202, data=bytes(8)), 0.0)
        ]

        with patch.object(mgr, "send", new_callable=AsyncMock) as send:
            activated = await mgr.activate_motor("m1")

        assert activated is False
        motor.activation_steps.assert_not_called()
        assert send.await_count == 0

    async def test_構成の食い違いは鮮度待ちより先に見る(self) -> None:
        """鮮度待ちを先に通すと、待っても解けない食い違いで 0.5 秒待たされる。

        しかもその間 `feedback_probe_message` (= disable) を打ち続けるので、
        「止めたい相手へ通信を続ける」形になる。
        """
        mgr, motor = self._prepare()
        motor.activation_block_reason.return_value = "p_max が食い違っています"
        motor.requires_fresh_feedback_for_activation.return_value = True
        motor.feedback_probe_message.return_value = can.Message(arbitration_id=0x203, data=bytes(8))

        with patch.object(mgr, "send", new_callable=AsyncMock) as send:
            activated = await mgr.activate_motor("m1", feedback_timeout_s=5.0)

        assert activated is False
        # 鮮度待ちへ入っていれば問い合わせが 1 通は飛ぶ
        assert send.await_count == 0

    async def test_activation_skipped_when_feedback_never_arrives(self) -> None:
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

    async def test_鮮度の判定は壁時計に依存しない(self) -> None:
        """NTP が時刻を後ろへ補正しても、届いたフィードバックは新規と認めること。

        `_last_rx_at` の大小で新規判定をしていた頃は、baseline を取った直後に
        時計が戻ると **届き続けているのに** 新規と認められずタイムアウトした
        (症状は「フィードバックを受信できないため有効化を見送りました」だけで、
        配線不良と区別が付かない)。
        """
        mgr, motor = self._prepare()
        motor.requires_fresh_feedback_for_activation.return_value = True
        motor.feedback_probe_message.return_value = can.Message(arbitration_id=0x203, data=bytes(8))
        motor.activation_steps.return_value = [
            (can.Message(arbitration_id=0x202, data=bytes(8)), 0.0)
        ]
        started = time.time()
        mark_feedback_at(mgr, "m1", started)

        async def fake_send(name: str, msg: can.Message) -> None:
            # 応答は届いているが、その間に時計が 10 秒巻き戻った状況
            mark_feedback_at(mgr, name, started - 10.0)

        with patch.object(mgr, "send", new_callable=AsyncMock, side_effect=fake_send):
            activated = await mgr.activate_motor("m1", feedback_timeout_s=0.5)

        assert activated is True
        motor.activation_steps.assert_called_once()

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
        mgr, motor = self._prepare()
        motor.activation_steps.return_value = [
            (can.Message(arbitration_id=0x202, data=bytes(8)), 0.0)
        ]

        with patch.object(mgr, "send", new_callable=AsyncMock) as send:
            inactive = await mgr.activate_motors(should_abort=lambda: True)

        assert send.await_count == 0
        assert inactive == ["m1"]

    async def test_activate_motors_continues_after_one_motor_fails(self) -> None:
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

    async def test_activate_motors_only_filters_target_motors(self) -> None:
        mgr = CANManager()
        mgr.add_bus("can0", mock_bus())
        enable_msg = can.Message(arbitration_id=0x202, data=bytes(8))
        for index, name in enumerate(("m1", "m2", "m3"), start=1):
            motor = mock_driver(name, index)
            motor.activation_steps.return_value = [(enable_msg, 0.0)]
            mgr.add_motor("can0", motor)

        with patch.object(mgr, "send", new_callable=AsyncMock) as send:
            inactive = await mgr.activate_motors(only={"m2"})

        assert inactive == []
        assert [call.args[0] for call in send.await_args_list] == ["m2"]

    async def test_feedback_probe_is_never_sent_to_an_energized_motor(self) -> None:
        sent: list[can.Message] = []
        mgr = CANManager(run_blocking=direct_runner())
        bus = mock_bus()
        bus.send.side_effect = lambda msg: sent.append(msg)
        mgr.add_bus("can_edulite", bus)
        dropped = Edulite05Driver("rotate_r", can_id=1)
        partner = Edulite05Driver("rotate_l", can_id=2)
        for driver in (dropped, partner):
            mgr.add_motor("can_edulite", driver)

        feed_edulite(dropped, position=0.5, mode_state=0)
        feed_edulite(partner, position=0.0, mode_state=2)

        await mgr.activate_motors(only={"rotate_r", "rotate_l"}, feedback_timeout_s=0.05)

        wire = [(msg.arbitration_id, bytes(msg.data)) for msg in sent]
        partner_probe = partner.encode_disable()
        assert (partner_probe.arbitration_id, bytes(partner_probe.data)) not in wire, (
            "励磁中の相方へ disable プローブが飛んでいる (保持トルクを失う)"
        )
        dropped_probe = dropped.encode_disable()
        assert (dropped_probe.arbitration_id, bytes(dropped_probe.data)) in wire, (
            "無励磁側へのプローブまで消えている (鮮度を確かめる手段が無くなる)"
        )

    async def test_energized_motor_still_activates_without_a_probe(self) -> None:
        sent: list[can.Message] = []
        mgr = CANManager(run_blocking=direct_runner())
        bus = mock_bus()
        bus.send.side_effect = lambda msg: sent.append(msg)
        mgr.add_bus("can_edulite", bus)
        partner = Edulite05Driver("rotate_l", can_id=2)
        mgr.add_motor("can_edulite", partner)
        feed_edulite(partner, position=0.0, mode_state=2)

        async def _refresher_reply() -> None:
            await asyncio.sleep(0.02)
            deliver_frame(mgr, "can_edulite", edulite_feedback(partner, position=0.0, mode_state=2))

        reply = asyncio.create_task(_refresher_reply())
        inactive = await mgr.activate_motors(only={"rotate_l"}, feedback_timeout_s=0.5)
        await reply

        assert inactive == []
        probe = partner.encode_disable()
        wire = [(msg.arbitration_id, bytes(msg.data)) for msg in sent]
        assert (probe.arbitration_id, bytes(probe.data)) not in wire

    async def test_probe_is_allowed_right_after_set_zero(self) -> None:
        sent: list[can.Message] = []
        mgr = CANManager(run_blocking=direct_runner())
        bus = mock_bus()
        bus.send.side_effect = lambda msg: sent.append(msg)
        mgr.add_bus("can_edulite", bus)
        motor = Edulite05Driver("rotate_l", can_id=2)
        mgr.add_motor("can_edulite", motor)
        feed_edulite(motor, position=0.0, mode_state=2)

        await mgr.activate_motor("rotate_l", feedback_timeout_s=0.05, after_set_zero=True)

        probe = motor.encode_disable()
        wire = [(msg.arbitration_id, bytes(msg.data)) for msg in sent]
        assert (probe.arbitration_id, bytes(probe.data)) in wire

    async def test_initialize_motors_continues_after_one_motor_fails(self) -> None:
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


class TestReenergizeAfterPowerLoss:
    """物理非常停止で **DM3520 の電源が数秒落ちた**後の再励磁。

    CTRL_MODE も固定小数点レンジもフラッシュへ保存されないので、復帰した個体は
    出荷値 (MIT モード / p_max 12.5) で立っている。**再励磁経路は長いあいだ
    `activate_motors` しか呼んでおらず**、起動時に読んだ p_max が残っていたので
    励磁ゲートも素通りしていた —— 2026-09-09 に機構を壊しかけた 80 倍の経路が、
    緊急停止を踏むたびに復活していた。

    実機に一番近い形で確かめる (mock ドライバでは `initialization_steps` と
    `reinitialization_steps` が別々の MagicMock になり、**両者が同じレジスタを
    持っていること自体が検証から落ちる**)。
    """

    def _prepare(
        self, device_p_max: float, *, accept_writes: bool = True
    ) -> tuple[CANManager, dict[str, float]]:
        """実機の代わりに応答を返す DM3520 を 1 台載せた manager。

        `device` の値を書き換えると「電源断でレジスタが出荷値へ戻った」を作れる。
        `accept_writes=False` は「レンジ外の値を書いたので実機が元の値を返す」。
        """
        mgr = CANManager(run_blocking=direct_runner())
        mgr.add_bus("can_dm3520", mock_bus())
        driver = Dm3520Driver(
            "sub_y_axis_m", can_id=0x01, master_id=0x11, p_max=1000.0, v_max=200.0, t_max=10.0
        )
        mgr.add_motor("can_dm3520", driver)
        device = {"p_max": device_p_max, "v_max": driver.v_max, "t_max": driver.t_max}
        registers = {
            Dm3520Driver.REG_P_MAX: "p_max",
            Dm3520Driver.REG_V_MAX: "v_max",
            Dm3520Driver.REG_T_MAX: "t_max",
        }

        async def _send(motor_name: str, msg: can.Message) -> None:
            data = bytes(msg.data)
            if msg.arbitration_id == Dm3520Driver.CONFIG_FRAME_ID and data[3] in registers:
                if data[2] == Dm3520Driver.CONFIG_WRITE and accept_writes:
                    device[registers[data[3]]] = struct.unpack("<f", data[4:8])[0]
                if data[2] == Dm3520Driver.CONFIG_READ:
                    deliver_frame(
                        mgr,
                        "can_dm3520",
                        dm3520_config_response(driver, data[3], device[registers[data[3]]]),
                    )
                return
            # 本機のフィードバックは問い合わせ駆動。自分宛の 1 通に 1 通返す
            deliver_frame(mgr, "can_dm3520", dm3520_feedback(driver, error=0))

        mgr.send = _send  # type: ignore[method-assign]
        return mgr, device

    async def test_電源断でレンジが戻っていたら再励磁で書き直す(self) -> None:
        """**操縦者が押せるボタンはこれだけ。** 再励磁で直らないと、人が別ツールで
        レジスタを書き直して起動し直すまで機体が動かせない (2026-09-09 に 3 回)。
        """
        mgr, device = self._prepare(1000.0)
        assert await mgr.initialize_motors() == []  # 起動時は一致している

        device["p_max"] = 12.5  # 物理非常停止でドライバの電源が落ちた

        inactive = await mgr.activate_motors(feedback_timeout_s=0.5)

        assert inactive == []
        assert device["p_max"] == pytest.approx(1000.0)

    async def test_書き直しても戻らなければ再励磁でも止める(self) -> None:
        """起動時に読んだ 1000 を信じたまま励磁すると、12.5 で送られた位置を
        1000 で復号した **80 倍の値**がそのまま保持目標に書かれる。書き込みを
        足してもゲートは残す。
        """
        mgr, device = self._prepare(1000.0, accept_writes=False)
        assert await mgr.initialize_motors() == []

        device["p_max"] = 12.5

        inactive = await mgr.activate_motors(feedback_timeout_s=0.5)

        assert inactive == ["sub_y_axis_m"]

    async def test_書き直しをフラッシュへ保存しない(self) -> None:
        """**寿命は約 1 万回。** 電源が入るたびに焼けば確実に潰れるので、揮発を
        承知のうえで毎回書き直す。"""
        mgr, device = self._prepare(1000.0)
        await mgr.initialize_motors()
        device["p_max"] = 12.5
        sent: list[can.Message] = []
        original = mgr.send

        async def _record(motor_name: str, msg: can.Message) -> None:
            sent.append(msg)
            await original(motor_name, msg)

        mgr.send = _record  # type: ignore[method-assign]

        await mgr.activate_motors(feedback_timeout_s=0.5)

        config_frames = [
            bytes(msg.data) for msg in sent if msg.arbitration_id == Dm3520Driver.CONFIG_FRAME_ID
        ]
        assert (Dm3520Driver.REG_P_MAX, Dm3520Driver.CONFIG_WRITE) in [
            (data[3], data[2]) for data in config_frames
        ]
        assert all(data[2] != Dm3520Driver.CONFIG_SAVE for data in config_frames)

    async def test_レンジが変わっていなければ再励磁できる(self) -> None:
        """**再初期化は「判断材料を集める」より前に置く。**

        後ろへ置くと、読み返した直後にそれを捨てることになり、電源が落ちて
        いない健全な機体まで「未確認」で永久に励磁できなくなる。
        """
        mgr, _device = self._prepare(1000.0)
        assert await mgr.initialize_motors() == []

        inactive = await mgr.activate_motors(feedback_timeout_s=0.2)

        assert inactive == []

    async def test_再励磁で制御モードを書き直す(self) -> None:
        """MIT モードのまま励磁すると `0x100` の位置指令が解釈されず、
        「励磁を名乗るのにトルクが出ない」。`is_energized()` も `is_fault()` も
        掛からないので、症状はシーケンスのタイムアウトだけになる。
        """
        mgr, _device = self._prepare(1000.0)
        await mgr.initialize_motors()
        sent: list[can.Message] = []
        original = mgr.send

        async def _record(motor_name: str, msg: can.Message) -> None:
            sent.append(msg)
            await original(motor_name, msg)

        mgr.send = _record  # type: ignore[method-assign]

        await mgr.activate_motors(feedback_timeout_s=0.2)

        writes = [
            bytes(msg.data)[3]
            for msg in sent
            if msg.arbitration_id == Dm3520Driver.CONFIG_FRAME_ID
            and bytes(msg.data)[2] == Dm3520Driver.CONFIG_WRITE
        ]
        assert writes == [Dm3520Driver.REG_CTRL_MODE]

    async def test_読み返しが落ちても再励磁は再試行で通る(self) -> None:
        """**再励磁のたびにレンジを捨てる以上、取りこぼしは毎回の再励磁に効く。**

        1 通で諦める実装だと、緊急停止を解除するたびに機体が無励磁のまま残る。
        """
        mgr, _device = self._prepare(1000.0)
        assert await mgr.initialize_motors() == []
        drops = 2
        original = mgr.send

        async def _lossy(motor_name: str, msg: can.Message) -> None:
            nonlocal drops
            data = bytes(msg.data)
            is_read = msg.arbitration_id == Dm3520Driver.CONFIG_FRAME_ID and (
                data[2] == Dm3520Driver.CONFIG_READ
            )
            if is_read and drops > 0:
                drops -= 1
                return
            await original(motor_name, msg)

        mgr.send = _lossy  # type: ignore[method-assign]

        inactive = await mgr.activate_motors(feedback_timeout_s=0.5)

        assert inactive == []
        assert drops == 0


class TestClearEStopLatches:
    def _manager(self) -> tuple[CANManager, MagicMock]:
        mgr = CANManager(run_blocking=direct_runner())
        bus = mock_bus()
        mgr.add_bus("can0", bus)
        return mgr, bus

    async def test_自作モタドラ以外へは1通も送らない(self) -> None:
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
        mgr, bus = self._manager()
        mgr.add_motor("can0", GenericDriver("board", 0x11, control_type=ControlMode.DUTY))

        await mgr.clear_e_stop_latches()

        sent = bus.send.call_args_list[0].args[0]
        expected = GenericDriver.encode_e_stop_clear(0x11)
        assert sent.arbitration_id == expected.arbitration_id
        assert bytes(sent.data) == bytes(expected.data)

    async def test_1台の送信失敗で残りを諦めない(self) -> None:
        mgr, bus = self._manager()
        mgr.add_motor("can0", GenericDriver("first", 0x11, control_type=ControlMode.DUTY))
        mgr.add_motor("can0", GenericDriver("second", 0x12, control_type=ControlMode.DUTY))
        bus.send.side_effect = [can.CanError("ACK が返らない"), None]

        uncleared = await mgr.clear_e_stop_latches()

        assert uncleared == ["first"]
        assert bus.send.call_count == 2


class TestReceiveLoopRobustness:
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

        assert other.state.position == pytest.approx(90.0, abs=0.1)
        assert mgr.last_feedback_at("y_axis_l") is not None
        assert mgr.last_feedback_at("y_axis_r") is None
        assert mgr._rx_error_count["can0"] == 1

    async def test_receive_loop_isolates_failing_matcher_to_one_motor(self) -> None:
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

        assert healthy.state.position == pytest.approx(90.0)
        assert mgr._rx_error_count["can0"] == 1

    async def test_receive_loop_survives_interface_down_and_resumes(self) -> None:
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

        assert motor.state.position == pytest.approx(90.0)
        assert mgr.last_feedback_at("gripper") is not None

    # `asyncio.sleep` を patch して回数を数えてはならない —— `lib.can_manager.asyncio` は
    # 共有のモジュールオブジェクトなので、差し替えると pytest-asyncio ごと停止する。
    async def test_receive_loop_backs_off_after_a_receive_failure(self) -> None:
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

        spin_free_limit = int(window_s / _RECV_RETRY_MIN_S) + 2
        assert bus.recv.call_count <= spin_free_limit, (
            f"失敗のたびに待たずに再試行している (recv 呼び出し {bus.recv.call_count} 回)"
        )
        assert bus.recv.call_count >= 1, "1 度も再試行していない"

    async def test_receive_loop_propagates_cancelled_error(self) -> None:
        mgr = CANManager(run_blocking=direct_runner())
        bus = mock_bus()

        class CancellingDriver(GenericDriver):
            def update_state(self, msg: can.Message) -> MotorState:
                raise asyncio.CancelledError

        motor = CancellingDriver("gripper", 0x01)
        mgr.add_bus("can0", bus)
        mgr.add_motor("can0", motor)
        self._drain_recv(
            bus, [generic_feedback(motor, position=90.0), generic_feedback(motor, position=45.0)]
        )

        await self._run_loop(mgr)

        assert bus.recv.call_count == 1
        assert mgr._rx_error_count["can0"] == 0

    async def test_受信断は降りずに必ず記録される(self, caplog: pytest.LogCaptureFixture) -> None:
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
        assert errors[0].exc_info is not None


class TestDuplicateRegistration:
    def test_duplicate_motor_name_is_rejected(self) -> None:
        mgr = CANManager()
        mgr.add_bus("can_generic", mock_bus())
        mgr.add_motor("can_generic", mock_driver("gripper", 0x01))

        with pytest.raises(ValueError) as excinfo:
            mgr.add_motor("can_generic", mock_driver("gripper", 0x02))

        assert "gripper" in str(excinfo.value)

    def test_duplicate_motor_name_across_buses_is_rejected(self) -> None:
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
        assert "can_generic" in message
        assert "0x01" in message
        assert "gripper" in message
        assert "wall" in message

    def test_same_can_id_on_different_bus_is_allowed(self) -> None:
        mgr = CANManager()
        mgr.add_bus("can_generic", mock_bus())
        mgr.add_bus("can_edulite", mock_bus())
        mgr.add_motor("can_generic", mock_driver("gripper", 0x01))
        mgr.add_motor("can_edulite", mock_driver("rotate_l", 0x01))

        assert mgr.motors["gripper"].can_id == mgr.motors["rotate_l"].can_id

    def test_rejected_motor_is_not_registered(self) -> None:
        mgr = CANManager()
        mgr.add_bus("can_generic", mock_bus())
        first = mock_driver("gripper", 0x01)
        mgr.add_motor("can_generic", first)

        with pytest.raises(ValueError):
            mgr.add_motor("can_generic", mock_driver("wall", 0x01))

        assert mgr._bus_motors["can_generic"] == [first]
        assert set(mgr._motors) == {"gripper"}


class TestReadOnlyViews:
    def _mgr(self) -> CANManager:
        mgr = CANManager()
        mgr.add_bus("can_m3508", mock_bus(), channel="vcan0")
        mgr.add_bus("can_generic", mock_bus(), channel="vcan1")
        mgr.add_motor("can_m3508", mock_driver("y_axis_r", 0x01))
        mgr.add_motor("can_m3508", mock_driver("y_axis_l", 0x02))
        mgr.add_motor("can_generic", mock_driver("gripper", 0x01))
        return mgr

    def test_motors_は宣言順を保つ(self) -> None:
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
    async def test_インタフェース断で降りず復帰後に受信を再開する(self) -> None:
        mgr = CANManager()
        bus = mock_bus()
        calls = {"n": 0}

        def recv(timeout: float) -> can.Message | None:
            calls["n"] += 1
            if calls["n"] <= 2:
                raise can.CanOperationError("Network is down [Error Code 100]")
            return None

        bus.recv.side_effect = recv
        mgr.add_bus("can0", bus)

        task = asyncio.create_task(mgr._receive_loop("can0"))
        await asyncio.sleep(0.15)

        assert not task.done(), "受信ループが降りている (断絶で死んではならない)"
        assert calls["n"] > 2, "復帰後に呼び直していない"

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    async def test_断絶中はヘルスがDOWNになり復帰でOKへ戻る(self) -> None:
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
        await asyncio.sleep(0.3)

        assert mgr.health().buses[0].rx_down is False
        assert mgr.health().buses[0].state is BusHealth.OK

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    async def test_タイムアウトは復帰の証拠にならない(self) -> None:
        mgr = CANManager()
        bus = mock_bus()
        calls = {"n": 0}

        def recv(timeout: float) -> can.Message | None:
            calls["n"] += 1
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
        mgr = CANManager()
        bus = mock_bus()
        bus.recv.side_effect = can.CanOperationError("Network is down")
        mgr.add_bus("can0", bus)

        task = asyncio.create_task(mgr._receive_loop("can0"))
        await asyncio.sleep(0.03)
        task.cancel()

        done, _pending = await asyncio.wait({task}, timeout=1.0)
        assert task in done, "cancel() が効いていない (CancelledError を握り潰している)"

    async def test_recvが投げたキャンセルも握り潰さない(self) -> None:
        mgr = CANManager(run_blocking=direct_runner())
        bus = mock_bus()
        bus.recv.side_effect = asyncio.CancelledError
        mgr.add_bus("can0", bus)

        task = asyncio.create_task(mgr._receive_loop("can0"))
        done, _pending = await asyncio.wait({task}, timeout=1.0)

        assert task in done, "recv 由来のキャンセルを握り潰している (止められない受信ループ)"
        with pytest.raises(asyncio.CancelledError):
            await task


class TestRxDownEpisodes:
    async def test_1回の途絶で1件だけ数える(self) -> None:
        mgr = CANManager()
        bus = mock_bus()
        bus.recv.side_effect = can.CanOperationError("Network is down [Error Code 100]")
        mgr.add_bus("can0", bus)

        task = asyncio.create_task(mgr._receive_loop("can0"))
        await asyncio.sleep(0.15)
        assert mgr.health().buses[0].rx_down_episodes == 1

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    async def test_復帰してもエピソード数は0に戻らない(self) -> None:
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
        assert mgr.health().buses[0].rx_down_episodes == 1

        state["phase"] = "up"
        await asyncio.sleep(0.3)

        assert mgr.health().buses[0].rx_down is False, "復帰が反映されていない"
        assert mgr.health().buses[0].rx_down_episodes == 1, (
            "復帰しただけでエピソード数が消えている (見落とし防止の値が意味を失う)"
        )

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    async def test_2回目の途絶で2件目を数える(self) -> None:
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
        assert mgr.health().buses[0].rx_down_episodes == 1

        state["phase"] = "up"
        await asyncio.sleep(0.2)
        assert mgr.health().buses[0].rx_down is False

        state["phase"] = "down"
        await asyncio.sleep(0.1)
        assert mgr.health().buses[0].rx_down is True
        assert mgr.health().buses[0].rx_down_episodes == 2, "2 回目の立ち上がりを数えていない"

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    def test_reset_rx_down_episodesで0に戻す(self) -> None:
        mgr = CANManager()
        mgr.add_bus("can0", mock_bus())
        mgr._record_rx_down("can0")
        assert mgr.health().buses[0].rx_down_episodes == 1

        mgr.reset_rx_down_episodes()

        assert mgr.health().buses[0].rx_down_episodes == 0
        assert mgr.health().buses[0].rx_down is True

    def test_reset_rx_down_episodesは全バスを対象にする(self) -> None:
        mgr = CANManager()
        mgr.add_bus("can0", mock_bus())
        mgr.add_bus("can1", mock_bus())
        mgr._record_rx_down("can0")
        mgr._record_rx_down("can1")
        mgr._record_rx_down("can1")

        mgr.reset_rx_down_episodes()

        snap = mgr.health()
        by_name = {b.name: b for b in snap.buses}
        assert by_name["can0"].rx_down_episodes == 0
        assert by_name["can1"].rx_down_episodes == 0


class TestReceiveLoopOnAPollableBus:
    async def _run_until_idle(self, mgr: CANManager, bus_name: str = "can0") -> None:
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

        assert motor.state.position == pytest.approx(30.0)
        assert mgr.last_feedback_at("gripper") is not None
        assert calls == []

    async def test_滞留を捌く途中で他のタスクが走る(self) -> None:
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
        mgr = CANManager(run_blocking=direct_runner())
        bus = ReadableBus()
        mgr.add_bus("can0", bus)
        bus.queue(*(can.CanOperationError("Network is down") for _ in range(20)))

        task = asyncio.create_task(mgr._receive_loop("can0"))
        for _ in range(5):
            await asyncio.sleep(0)

        loop = asyncio.get_running_loop()
        assert loop.remove_reader(bus.fileno()) is False, (
            "復帰待ちのあいだ可読の監視を載せたままにしている (イベントループが空回りする)"
        )
        assert mgr.health().buses[0].state is BusHealth.DOWN

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    async def test_監視できないバスはエグゼキュータ経路へ落ちる(self) -> None:
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
        assert calls, "virtual バスでエグゼキュータを経由していない"


class TestSpuriousReadable:
    """readable の通知が来ても recvmsg に何も無いことがある。そこで待つと
    イベントループごと固まる (2026-09-09 実機: 非常停止中に Web が無応答)。"""

    async def test_EAGAIN_は受信断ではなく空振りとして扱う(self) -> None:
        import errno

        from tests.fake_can import ReadableBus

        mgr = CANManager()
        bus = ReadableBus()
        bus.queue(can.CanOperationError("Error receiving: EAGAIN", errno.EAGAIN))
        mgr.add_bus("can0", bus)  # type: ignore[arg-type]

        task = asyncio.create_task(mgr._receive_loop("can0"))
        await asyncio.sleep(0.05)

        assert mgr._rx_down.get("can0") is False
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        bus.shutdown()

    async def test_readable_監視に載せるソケットは非ブロッキングにする(self) -> None:
        import socket

        from lib.can_manager import _ReadableFd

        left, right = socket.socketpair()
        try:
            bus = MagicMock()
            bus.fileno.return_value = right.fileno()
            readable = _ReadableFd.for_bus(bus)
            assert readable is not None
            readable.close()
        finally:
            left.close()
            right.close()

        bus.socket.setblocking.assert_called_once_with(False)
