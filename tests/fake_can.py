from __future__ import annotations

import asyncio
import contextlib
import socket
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import can

from lib.can_manager import CANManager
from lib.drivers.base import FULL_TELEMETRY, MotorState
from tests.fake_health import ok_health_snapshot

DEFAULT_MOTOR_STATE = MotorState(position=0.0, velocity=0.0, current=0.0, temperature=30.0)


def mock_motor(name: str, state: MotorState | None = None) -> MagicMock:
    motor = MagicMock()
    motor.name = name
    motor.state = state if state is not None else DEFAULT_MOTOR_STATE
    motor.telemetry = FULL_TELEMETRY
    return motor


def mock_can_manager(
    motors: Mapping[str, MotorState | MagicMock] | Iterable[str] = ("m1",),
    *,
    bus_name: str = "bus0",
) -> CANManager:
    if isinstance(motors, Mapping):
        drivers = {
            name: value if isinstance(value, MagicMock) else mock_motor(name, value)
            for name, value in motors.items()
        }
    else:
        drivers = {name: mock_motor(name) for name in motors}

    mgr = MagicMock(spec=CANManager)
    mgr.motors = drivers
    mgr.sensors = {}
    mgr.bus_names = (bus_name,)
    mgr.send = AsyncMock()
    mgr.send_to_bus = AsyncMock()
    mgr.activate_motors = AsyncMock(return_value=[])
    mgr.clear_e_stop_latches = AsyncMock(return_value=[])
    mgr.last_feedback_at.return_value = None
    mgr.health.side_effect = lambda **_kwargs: ok_health_snapshot(mgr)
    return mgr


def mock_bus() -> MagicMock:
    bus = MagicMock()
    bus.recv.return_value = None
    return bus


class ReadableBus:
    def __init__(self, messages: Iterable[can.Message] = ()) -> None:
        self._trigger, self._watched = socket.socketpair()
        self._queue: deque[can.Message | Exception] = deque(messages)
        self.recv_calls = 0
        self._signalled = False
        self._notify()

    def fileno(self) -> int:
        return self._watched.fileno()

    def recv(self, timeout: float | None = None) -> can.Message | None:
        self.recv_calls += 1
        if not self._queue:
            return None
        item = self._queue.popleft()
        if not self._queue:
            self._consume()
        if isinstance(item, Exception):
            raise item
        return item

    def shutdown(self) -> None:
        self._trigger.close()
        self._watched.close()

    def queue(self, *items: can.Message | Exception) -> None:
        self._queue.extend(items)
        self._notify()

    def _notify(self) -> None:
        # socketpair は在庫 1 通につき 1 バイトではなく「読める / 読めない」の 1 バイトで表す。
        # 1 通ごとに送ると受信バッファに残ったバイトで永久に readable のままになる。
        if self._queue and not self._signalled:
            self._trigger.send(b"\0")
            self._signalled = True

    def _consume(self) -> None:
        if not self._signalled:
            return
        self._watched.setblocking(False)
        with contextlib.suppress(BlockingIOError, OSError):
            self._watched.recv(64)
        self._signalled = False


def mock_driver(name: str, can_id: int) -> MagicMock:
    motor = MagicMock()
    motor.name = name
    motor.can_id = can_id
    motor.matches_feedback.return_value = False
    motor.update_state.return_value = MotorState()
    motor.telemetry = FULL_TELEMETRY
    motor.initialization_steps.return_value = []
    motor.activation_steps.return_value = []
    motor.reinitialization_steps.return_value = []
    motor.requires_fresh_feedback_for_activation.return_value = False
    motor.feedback_probe_message.return_value = None
    # 空リスト = 「確認すべき設定は無い」。MagicMock の既定 (真) とは逆の意味になる
    motor.configuration_probe_messages.return_value = []
    # **None は「励磁を止める理由なし」で、MagicMock の既定 (真) とは逆の意味になる。**
    # 明示しないと、励磁を止める口を足した瞬間に全モータが無励磁のまま残る
    motor.activation_block_reason.return_value = None
    # **False は「励磁中だと分かっていない」。** MagicMock の既定 (真) のままだと、
    # 励磁中のモータへ送らない手当て (再初期化・鮮度確認の問い合わせ) が全部止まる
    motor.is_energized.return_value = None
    return motor


def direct_runner(
    record: list[tuple[Any, tuple[Any, ...]]] | None = None,
) -> Callable[..., Awaitable[Any]]:

    async def run(func: Callable[..., Any], *args: Any) -> Any:
        if record is not None:
            record.append((func, args))
        await asyncio.sleep(0)
        return func(*args)

    return run


def set_motors(mgr: CANManager, drivers: Mapping[str, object]) -> None:
    mgr.motors = dict(drivers)


def set_sensors(mgr: CANManager, drivers: Mapping[str, object]) -> None:
    mgr.sensors = dict(drivers)


def set_last_feedback(mgr: CANManager, times: Mapping[str, float]) -> None:
    mgr.last_feedback_at.side_effect = lambda name: times.get(name)


def keep_feedback_fresh(mgr: CANManager, names: Iterable[str] | None = None) -> None:
    """モックの鮮度を常に「今」にする。実時間で待つテストが途中で途絶に化けないため。"""
    fresh = None if names is None else set(names)
    mgr.last_feedback_at.side_effect = lambda name: (
        time.time() if fresh is None or name in fresh else None
    )


def deliver_frame(mgr: CANManager, bus_name: str, msg: can.Message) -> None:
    mgr._dispatch_frame(bus_name, mgr._bus_motors[bus_name], msg)


def mark_feedback_at(mgr: CANManager, motor_name: str, at: float) -> None:
    mgr._last_rx_at[motor_name] = at
    mgr._rx_seq[motor_name] = mgr._rx_seq.get(motor_name, 0) + 1


def mark_bus_off(mgr: CANManager, bus_name: str, *, value: bool = True) -> None:
    mgr._bus_off[bus_name] = value
