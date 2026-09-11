from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Iterable
from typing import Any
from unittest.mock import AsyncMock, patch

from aiohttp import web

from lib.can_manager import CANManager
from lib.commands import COMMANDS
from lib.control.limit_monitor import LimitMonitor
from lib.control.position_loop import M3508PositionLoop
from lib.control.sync_monitor import SyncMonitor
from lib.control.target_refresh import GenericTargetRefresher
from lib.health import HealthSnapshot
from lib.manual import ManualController
from lib.match_state import Court, MatchState
from lib.sequence.engine import Sequence
from lib.server import _ENERGIZE_GRACE_S, _FIRMWARE_INFO_GRACE_S, RobotServer
from lib.suction import SuctionSelection
from tests.fake_can import mock_can_manager

FROZEN_BROADCAST_INTERVAL_S = 3600.0


class ServerFixture:
    def __init__(self, server: RobotServer) -> None:
        self.server = server
        self._sequences: dict[str, Any] = {}
        self._can_managers: dict[str, Any] = {}
        self._manuals: dict[str, Any] = {}

    @classmethod
    def build(cls, **server_kwargs: Any) -> ServerFixture:
        return cls(RobotServer(**server_kwargs))

    def add_robot(
        self,
        name: str,
        sequence: Sequence,
        can_manager: CANManager | None = None,
        *,
        position_loops: list[M3508PositionLoop] | None = None,
        sync_monitors: list[SyncMonitor] | None = None,
        limit_monitors: list[LimitMonitor] | None = None,
        target_refreshers: list[GenericTargetRefresher] | None = None,
        manual: ManualController | None = None,
        suction: SuctionSelection | None = None,
    ) -> Any:
        mgr = can_manager if can_manager is not None else mock_can_manager()
        self.server.add_robot(
            name,
            sequence,
            mgr,
            position_loops=position_loops,
            sync_monitors=sync_monitors,
            limit_monitors=limit_monitors,
            target_refreshers=target_refreshers,
            manual=manual,
            suction=suction,
        )
        self._sequences[name] = sequence
        self._can_managers[name] = mgr
        self._manuals[name] = manual
        return mgr

    def create_app(self) -> web.Application:
        return self.server.create_app()

    @property
    def robot_names(self) -> tuple[str, ...]:
        return tuple(self._sequences)

    def sequence(self, name: str) -> Any:
        return self._sequences[name]

    def sequences(self) -> list[Any]:
        return list(self._sequences.values())

    def can_manager(self, name: str) -> Any:
        return self._can_managers[name]

    def can_managers(self) -> list[Any]:
        return list(self._can_managers.values())

    def manual(self, name: str) -> Any:
        return self._manuals[name]

    def operation_mode(self, name: str) -> str:
        return self.state_message(name)["manual"]["mode"]

    @property
    def match(self) -> MatchState:
        return self.server.match

    @property
    def e_stop_active(self) -> bool:
        return self.server.e_stop_active

    async def command(self, payload: dict, *, requester: Any = None) -> None:
        await self.server.handle_command(payload, requester=requester)

    async def activate_e_stop(self, *, reason: str | None = None) -> None:
        await self.server.activate_e_stop(reason=reason)

    async def wait_reactivation(self, *, timeout: float = 2.0) -> None:
        tasks = {task for task in self.server._reactivate_tasks if not task.done()}
        if not tasks:
            return
        done, _still_running = await asyncio.wait(tasks, timeout=timeout)
        assert len(done) == len(tasks), f"解除の再励磁タスクが {timeout}s 以内に終わっていない"

    def expire_firmware_grace(self) -> None:
        self.server._server_started_at = time.time() - _FIRMWARE_INFO_GRACE_S - 0.1

    def expire_energize_grace(self) -> None:
        self.server._energize_expected_since = time.time() - _ENERGIZE_GRACE_S - 0.1

    async def wait_reenergize(self, robot_name: str, *, timeout: float = 2.0) -> None:
        task = self.server._reenergize_tasks.get(robot_name)
        if task is None or task.done():
            return
        done, _still_running = await asyncio.wait({task}, timeout=timeout)
        assert done, f"再励磁タスクが {timeout}s 以内に終わっていない: robot={robot_name}"

    def has_pending_reenergize(self, robot_name: str) -> bool:
        return robot_name in self.server._reenergize_tasks

    def set_motor_check_task(self, task: asyncio.Task[None] | None) -> None:
        self.server._motor_check._task = task

    def set_homing_source(self, source: Any) -> None:
        self.server.set_homing_source(source)

    async def start_homing(self, robot: str, axes: list[str] | None = None) -> str | None:
        return await self.server._homing.start(robot, axes)

    def homing_state(self) -> dict:
        return self.server._homing.payload()

    def set_homing_running(self, running: bool, robot: str = "sub_hand") -> None:
        self.server._homing._run_of(robot).running = running

    async def wait_homing_idle(self, *, timeout: float = 2.0) -> None:
        tasks = [run.task for run in self.server._homing._runs.values() if run.task is not None]
        if not tasks:
            return
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=timeout)

    async def start_switch_measure(self, payload: dict) -> str | None:
        return await self.server._switch_measure.start(payload)

    def switch_measure_state(self) -> dict:
        return self.server._switch_measure.payload()

    def set_switch_measure_running(self, running: bool) -> None:
        self.server._switch_measure._running = running

    async def wait_switch_measure_idle(self, *, timeout: float = 2.0) -> None:
        task = self.server._switch_measure._task
        if task is None:
            return
        await asyncio.wait_for(task, timeout=timeout)

    def break_command_handler(self, command: str, exc: Exception) -> None:

        async def _raise(_data: dict, _requester: Any) -> None:
            raise exc

        setattr(self.server, COMMANDS[command].handler, _raise)

    def make_ready(self, court: Court = Court.RED) -> None:
        self.match.set_court(court)

    def enter_match(self) -> None:
        self.make_ready()
        assert self.match.match_start(), "READY に到達していないため試合へ入れない"

    def freeze_broadcast(self) -> None:
        self.server._broadcast_interval = FROZEN_BROADCAST_INTERVAL_S

    def set_broadcast_interval(self, seconds: float) -> None:
        self.server._broadcast_interval = seconds

    async def publish_state(self) -> None:
        await self.server._broadcast_state()

    def state_message(self, robot: str) -> dict:
        return self.server._build_state_message(robot)

    def health(self, robot: str) -> HealthSnapshot:
        return self.server._compute_health(robot)

    async def publish(self, payload: dict) -> None:
        await self.server._ws.broadcast_json(payload)

    async def fanout(self, payloads: list[dict]) -> None:
        await self.server._ws.fanout(payloads)

    async def publish_e_stop_state(self) -> None:
        await self.server._broadcast_e_stop_state()

    async def publish_motor_check_state(self) -> None:
        await self.server._motor_check.publish()

    async def publish_switch_measure_state(self) -> None:
        await self.server._switch_measure.publish()

    async def publish_motor_check_error(self, message: str) -> None:
        await self.server._motor_check.report_error(message)

    def broadcast_loop(self) -> Any:
        return self.server._broadcast_loop()

    def patch_publish_state(self, replacement: Callable[[], Any]) -> None:
        self.server._broadcast_state = replacement  # type: ignore[method-assign]

    def patch_e_stop_broadcast(self) -> Any:
        return patch.object(self.server, "_broadcast_e_stop_state", new_callable=AsyncMock)

    @staticmethod
    def shrink_ws_send_timeout(monkeypatch: Any, seconds: float = 0.05) -> None:
        monkeypatch.setattr("lib.ws_hub._WS_SEND_TIMEOUT_S", seconds)

    def attach_clients(self, *clients: Any) -> None:
        self.server._ws._clients = set(clients)

    def connect_client(self, client: Any) -> None:
        self.server._ws._clients.add(client)

    def is_connected(self, client: Any) -> bool:
        return client in self.server._ws._clients

    async def run_ws_handler(self, client: Any) -> Any:
        with patch("lib.server.web.WebSocketResponse", return_value=client):
            return await self.server._ws_handler(AsyncMock())

    @property
    def client_count(self) -> int:
        return len(self.server._ws._clients)

    def only_client(self) -> Any:
        clients = self.server._ws._clients
        assert len(clients) == 1, f"接続中のクライアントが 1 台ではない: {len(clients)}"
        return next(iter(clients))

    @property
    def has_closing_tasks(self) -> bool:
        return bool(self.server._ws._closing_tasks)

    async def shutdown(self, app: web.Application) -> None:
        await self.server._on_shutdown(app)

    async def handle_match_start(self, requester: Any) -> None:
        await self.server._handle_match_start(requester)

    def set_motor_check_sequence(self, sequence: Any) -> None:
        self.server.set_motor_check_sequence(sequence)

    async def start_motor_check(self) -> bool:
        return await self.server._motor_check.start()

    def set_motor_check_pausables(self, pausables: list[Any]) -> None:
        self.server._motor_check._pausables = lambda: list(pausables)

    def motor_check_pausables(self) -> list[Any]:
        return self.server._motor_check_pausables()

    def abort_motor_check(self) -> None:
        self.server._motor_check.abort()

    def motor_check_sequence(self) -> Any:
        return self.server._motor_check.sequence

    def motor_check_state(self) -> dict:
        return self.server._motor_check.payload()

    def motor_check_error(self) -> str | None:
        return self.server._motor_check.error

    async def wait_motor_check_idle(self, *, timeout: float = 2.0) -> None:
        task = self.server._motor_check._task
        if task is None:
            return
        await asyncio.wait_for(task, timeout=timeout)

    async def wait_motor_check_running(self, *, timeout: float = 2.0) -> Any:
        sequence = self.server._motor_check.sequence
        assert sequence is not None, "動作確認シーケンスが登録されていない"

        assert await wait_until(lambda: sequence.is_running, timeout=timeout), (
            "動作確認シーケンスが起動しなかった"
        )
        return sequence

    def set_position_loops(self, robot: str, loops: list[M3508PositionLoop]) -> None:
        self.server._robots[robot].position_loops = loops

    def set_target_refreshers(self, robot: str, refreshers: list[GenericTargetRefresher]) -> None:
        self.server._robots[robot].target_refreshers = refreshers

    def position_loops(self, robot: str) -> list[M3508PositionLoop]:
        return self.server._robots[robot].position_loops

    def target_refreshers(self, robot: str) -> list[GenericTargetRefresher]:
        return self.server._robots[robot].target_refreshers


class RecordingClient:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed = False

    async def send_str(self, payload: str) -> None:
        self.sent.append(payload)

    async def close(self) -> None:
        self.closed = True

    def messages(self) -> list[dict]:
        return [json.loads(msg) for msg in self.sent]

    def types(self) -> list[str]:
        return [msg["type"] for msg in self.messages()]

    def of_type(self, name: str) -> list[dict]:
        return [msg for msg in self.messages() if msg.get("type") == name]


async def wait_until(predicate: Callable[[], bool], *, timeout: float = 2.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


async def recv_type(ws: Any, wanted: str, *, tries: int = 40, timeout: float = 0.2) -> dict | None:
    for _ in range(tries):
        try:
            msg = await asyncio.wait_for(ws.receive_json(), timeout=timeout)
        except (TimeoutError, TypeError):
            return None
        if msg.get("type") == wanted:
            return msg
    return None


async def require_type(ws: Any, wanted: str, *, tries: int = 40, timeout: float = 0.5) -> dict:
    msg = await recv_type(ws, wanted, tries=tries, timeout=timeout)
    if msg is None:
        raise AssertionError(f"{wanted} が配信されなかった")
    return msg


async def expect_no_type(ws: Any, unwanted: str, *, tries: int = 8, timeout: float = 0.1) -> None:
    for _ in range(tries):
        try:
            msg = await asyncio.wait_for(ws.receive_json(), timeout=timeout)
        except (TimeoutError, TypeError):
            return
        assert msg.get("type") != unwanted, f"{unwanted} が配信された: {msg}"


async def drain(ws: Any, *, timeout: float = 0.05, limit: int = 50) -> list[dict]:
    drained: list[dict] = []
    for _ in range(limit):
        try:
            msg = await asyncio.wait_for(ws.receive_json(), timeout=timeout)
        except (TimeoutError, TypeError):
            break
        drained.append(msg)
    return drained


async def collect_types(ws: Any, wanted: Iterable[str], *, tries: int = 60) -> list[dict]:
    wanted_set = set(wanted)
    found: list[dict] = []
    for _ in range(tries):
        try:
            msg = await asyncio.wait_for(ws.receive_json(), timeout=0.2)
        except (TimeoutError, TypeError):
            break
        if msg.get("type") in wanted_set:
            found.append(msg)
    return found


def seed_jitter_overrun(task: Any, *, count: int = 1, worst_s: float = 0.05) -> None:
    task._jitter_overrun_count = count
    task._worst_jitter_s = worst_s
