"""RobotServer をテストから組み立て・駆動するための唯一の入口。

``RobotServer`` のテストは 10 ファイルに分かれているが、どれも同じことをする:
サーバーを建て、モックの CAN 層を挿し、配信を 1 回だけ走らせ、結果を見る。
それが各ファイルに書き写されていたため、サーバーの構造を 1 つ変えるだけで
10 ファイルが機械的に赤くなり、そのたびに「モックの追従」としてテストを実装へ
合わせ直していた。テストは実装の変更を検出するための網なのに、変更のたびに
網を編み直しては何も守れない。組み立てと駆動をここへ集約する。

**サーバー内部 (`_broadcast_state` などの private) へ手を伸ばすのは本ファイルの
特権とする。** 公開 API で書けるものは公開 API を使う (``e_stop_active`` /
``handle_command`` / ``activate_e_stop`` / ``match``)。ここに残る private 参照は
「テストが配信や動作確認のタイミングを決定的に握るために要るが、本番の
呼び出し元を増やしたくない」ものだけで、サーバーの構造が変わったときの
追従はこのファイルだけで済む。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from typing import Any
from unittest.mock import AsyncMock, patch

from aiohttp import web

from lib.can_manager import CANManager
from lib.control.position_loop import M3508PositionLoop
from lib.control.sync_monitor import SyncMonitor
from lib.control.target_refresh import GenericTargetRefresher
from lib.health import CheckRunSnapshot, HealthSnapshot
from lib.match_state import MatchState
from lib.motor_check import MotorCheckRunner
from lib.sequence.engine import Sequence
from lib.server import RobotServer
from tests.fake_can import mock_can_manager

#: 周期配信に割り込まれるとヘルス差分の基準が動く。テストが明示的に呼んだ配信
#: だけを観測したいときは、この間隔まで伸ばして実質止める
FROZEN_BROADCAST_INTERVAL_S = 3600.0


class ServerFixture:
    """1 台の ``RobotServer`` と、そこへ登録したロボット一式。

    登録したシーケンス・CAN マネージャはフィクスチャ側が覚えておく。
    サーバーへ問い合わせ直す必要が無くなり、``RobotServer`` がロボットを
    どう保持しているか (``_robots``) にテストが依存しなくなる。
    """

    def __init__(self, server: RobotServer) -> None:
        self.server = server
        self._sequences: dict[str, Any] = {}
        self._can_managers: dict[str, Any] = {}

    # ------------------------------------------------------------------ #
    #  構築
    # ------------------------------------------------------------------ #

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
        target_refreshers: list[GenericTargetRefresher] | None = None,
    ) -> Any:
        mgr = can_manager if can_manager is not None else mock_can_manager()
        self.server.add_robot(
            name,
            sequence,
            mgr,
            position_loops=position_loops,
            sync_monitors=sync_monitors,
            target_refreshers=target_refreshers,
        )
        self._sequences[name] = sequence
        self._can_managers[name] = mgr
        return mgr

    def create_app(self) -> web.Application:
        return self.server.create_app()

    # ------------------------------------------------------------------ #
    #  登録済みロボットの参照 (サーバーへ問い合わせず控えから返す)
    # ------------------------------------------------------------------ #

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

    # ------------------------------------------------------------------ #
    #  公開 API の素通し (テスト側の記述を短くするだけ)
    # ------------------------------------------------------------------ #

    @property
    def match(self) -> MatchState:
        return self.server.match

    @property
    def e_stop_active(self) -> bool:
        return self.server.e_stop_active

    async def command(self, payload: dict, *, requester: Any = None) -> None:
        """コマンドを受理経路へ流す (WS も内部呼び出しもここへ合流する)。"""
        await self.server.handle_command(payload, requester=requester)

    async def activate_e_stop(self, *, reason: str | None = None) -> None:
        await self.server.activate_e_stop(reason=reason)

    # ------------------------------------------------------------------ #
    #  試合フェーズ
    # ------------------------------------------------------------------ #

    def complete_checklist(self, role: str) -> None:
        for item in self.match.checklists[role].items:
            self.match.set_checklist_item(role, item.id, True)

    def complete_all_checklists(self) -> None:
        for role in self.match.checklists:
            self.complete_checklist(role)

    def enter_match(self) -> None:
        """指差喚呼を全て通して試合中まで進める (フェーズゲートの前提を作る)。"""
        self.complete_all_checklists()
        assert self.match.match_start(), "READY に到達していないため試合へ入れない"

    # ------------------------------------------------------------------ #
    #  配信 — テストが 1 フレームずつ決定的に進めるための seam
    # ------------------------------------------------------------------ #

    def freeze_broadcast(self) -> None:
        """周期配信を実質止める。テストが明示的に呼んだ配信だけを観測したいとき用。"""
        self.server._broadcast_interval = FROZEN_BROADCAST_INTERVAL_S

    def set_broadcast_interval(self, seconds: float) -> None:
        self.server._broadcast_interval = seconds

    async def publish_state(self) -> None:
        """テレメトリを 1 フレームぶんだけ配信する (周期ループを待たない)。"""
        await self.server._broadcast_state()

    def state_message(self, robot: str) -> dict:
        """配信される state メッセージを WS を介さず 1 通組み立てる。"""
        return self.server._build_state_message(robot)

    def health(self, robot: str) -> HealthSnapshot:
        return self.server._compute_health(robot)

    async def publish(self, payload: dict) -> None:
        await self.server._broadcast_json(payload)

    async def fanout(self, payloads: list[dict]) -> None:
        await self.server._fanout(payloads)

    async def publish_e_stop_state(self) -> None:
        await self.server._broadcast_e_stop_state()

    async def publish_motor_check_progress(
        self, robot: str, motor: str, index: int, total: int
    ) -> None:
        await self.server._broadcast_motor_check_progress(robot, motor, index, total)

    async def publish_motor_check_record(self, robot: str, record: Any) -> None:
        await self.server._broadcast_motor_check_record(robot, record)

    async def publish_motor_check_done(self, robot: str, snapshot: CheckRunSnapshot) -> None:
        await self.server._broadcast_motor_check_done(robot, snapshot)

    async def publish_motor_check_error(self, robot: str, message: str) -> None:
        await self.server._broadcast_motor_check_error(robot, message)

    def broadcast_loop(self) -> Any:
        """配信ループのコルーチン。1 回の例外で止まらないことを見るテスト用。"""
        return self.server._broadcast_loop()

    def patch_publish_state(self, replacement: Callable[[], Any]) -> None:
        self.server._broadcast_state = replacement  # type: ignore[method-assign]

    def patch_e_stop_broadcast(self) -> Any:
        """緊急停止の配信だけを差し替えるコンテキストマネージャ。

        停止フレームの送信経路を見るテストが、配信の中身まで巻き込まないようにする。
        """
        return patch.object(self.server, "_broadcast_e_stop_state", new_callable=AsyncMock)

    # ------------------------------------------------------------------ #
    #  WS クライアント
    #
    #  「送信が永久に返らない」「送信が例外を投げる」相手は実ソケットでは
    #  作れないため、偽クライアントを直接ぶら下げる。配信の不変条件
    #  (1 台の不調で全員のテレメトリを止めない) はこれでしか検証できない。
    # ------------------------------------------------------------------ #

    def attach_clients(self, *clients: Any) -> None:
        self.server._ws_clients = set(clients)

    def connect_client(self, client: Any) -> None:
        """配信の最中に 1 台繋がってきた状況を作る (``_ws_handler`` の ``add`` 相当)。

        実機では操縦者がリロードするだけで起きる。配信は ``await`` を挟むので、
        その隙にハンドラが同じ集合を書き換える。
        """
        self.server._ws_clients.add(client)

    def is_connected(self, client: Any) -> bool:
        return client in self.server._ws_clients

    @property
    def client_count(self) -> int:
        return len(self.server._ws_clients)

    def only_client(self) -> Any:
        """接続中のクライアントが 1 台だけであることを確認して返す。"""
        clients = self.server._ws_clients
        assert len(clients) == 1, f"接続中のクライアントが 1 台ではない: {len(clients)}"
        return next(iter(clients))

    @property
    def has_closing_tasks(self) -> bool:
        """切り離したクライアントを閉じるタスクの参照が残っているか (GC 対策)。"""
        return bool(self.server._closing_tasks)

    async def shutdown(self, app: web.Application) -> None:
        await self.server._on_shutdown(app)

    async def handle_match_start(self, requester: Any) -> None:
        """フェーズゲートを迂回して match_start の防御的判定だけを踏む。"""
        await self.server._handle_match_start(requester)

    # ------------------------------------------------------------------ #
    #  アクチュエータ動作確認
    # ------------------------------------------------------------------ #

    async def start_motor_check(self, robot: str) -> bool:
        """動作確認を起動する。拒否されたら False (HTTP POST の 409 と同じ判定)。"""
        return await self.server._start_motor_check(robot)

    def motor_check_runner(self, robot: str) -> Any:
        return self.server._motor_check_runners.get(robot)

    def install_motor_check_runner(self, robot: str, runner: Any) -> None:
        self.server._motor_check_runners[robot] = runner

    def last_motor_check(self, robot: str) -> CheckRunSnapshot | None:
        return self.server._motor_check_last.get(robot)

    async def wait_motor_check_idle(self, robot: str, *, timeout: float = 2.0) -> None:
        task = self.server._motor_check_tasks.get(robot)
        if task is None:
            return
        await asyncio.wait_for(task, timeout=timeout)

    async def wait_motor_check_running(
        self, robot: str, *, timeout: float = 2.0
    ) -> MotorCheckRunner:
        runner: Any = None

        def _running() -> bool:
            nonlocal runner
            runner = self.motor_check_runner(robot)
            return runner is not None and runner.is_running

        assert await wait_until(_running, timeout=timeout), "動作確認 runner が起動しなかった"
        return runner

    # ------------------------------------------------------------------ #
    #  ロボット配線の後付け
    #  (位置制御ループ・目標値再送は CAN マネージャを要るため後から挿す)
    # ------------------------------------------------------------------ #

    def set_position_loops(self, robot: str, loops: list[M3508PositionLoop]) -> None:
        self.server._robots[robot].position_loops = loops

    def set_target_refreshers(self, robot: str, refreshers: list[GenericTargetRefresher]) -> None:
        self.server._robots[robot].target_refreshers = refreshers

    def position_loops(self, robot: str) -> list[M3508PositionLoop]:
        return self.server._robots[robot].position_loops

    def target_refreshers(self, robot: str) -> list[GenericTargetRefresher]:
        return self.server._robots[robot].target_refreshers


# ---------------------------------------------------------------------- #
#  待ち合わせ / WS 受信ヘルパ (5 ファイルに同じものが書き写されていた)
# ---------------------------------------------------------------------- #


async def wait_until(predicate: Callable[[], bool], *, timeout: float = 2.0) -> bool:
    """条件成立をポーリングで待つ (固定 sleep より取りこぼしに強い)。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


async def recv_type(ws: Any, wanted: str, *, tries: int = 40, timeout: float = 0.2) -> dict | None:
    """周期配信の state に紛れた特定 type のメッセージを 1 通拾う。"""
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
    """一定時間その type が流れてこないことを確認する。"""
    for _ in range(tries):
        try:
            msg = await asyncio.wait_for(ws.receive_json(), timeout=timeout)
        except (TimeoutError, TypeError):
            return
        assert msg.get("type") != unwanted, f"{unwanted} が配信された: {msg}"


async def drain(ws: Any, *, timeout: float = 0.05, limit: int = 50) -> list[dict]:
    """WS の残メッセージを排出する。タイムアウトしたら戻る。"""
    drained: list[dict] = []
    for _ in range(limit):
        try:
            msg = await asyncio.wait_for(ws.receive_json(), timeout=timeout)
        except (TimeoutError, TypeError):
            break
        drained.append(msg)
    return drained


async def collect_types(ws: Any, wanted: Iterable[str], *, tries: int = 60) -> list[dict]:
    """指定 type のメッセージを取りこぼさず集める (順序は到着順)。"""
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
