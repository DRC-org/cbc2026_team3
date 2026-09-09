from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import pathlib
import time
from collections import deque
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass, field

from aiohttp import WSMsgType, web

from lib import server_dryrun
from lib.can_manager import CANManager
from lib.commands import COMMANDS, CommandSpec, RejectChannel, spec_for
from lib.config_schema import (
    DEFAULT_HEALTH,
    DEFAULT_MATCH,
    HealthThresholds,
    MatchSettings,
)
from lib.control.feedback import FeedbackFreshness
from lib.control.limit_guard import LimitGuard
from lib.control.periodic import PeriodicTask
from lib.control.position_loop import M3508PositionLoop
from lib.control.sync_monitor import SyncMonitor
from lib.control.target_refresh import TargetRefresher
from lib.drivers.base import TelemetrySupport
from lib.drivers.generic import GenericDriver
from lib.health import (
    BusHealth,
    BusHealthInfo,
    HealthSnapshot,
    MotorHealth,
    MotorHealthInfo,
    worst_bus_health,
)
from lib.manual import ManualControlError, ManualController, OperationMode
from lib.match_state import ChecklistItem, Court, MatchState
from lib.sequence.engine import Sequence
from lib.server_motor_check import MotorCheckController, Pausable
from lib.ws_hub import WsHub

logger = logging.getLogger(__name__)

_WEB_DIST_DIR = pathlib.Path(__file__).resolve().parent.parent / "web" / "dist"


_FAILED_TASK_BACKLOG = 5

_ENERGIZE_GRACE_S = 0.5

_FIRMWARE_INFO_GRACE_S = 3.0

_PENDING_TASK_CANCEL_TIMEOUT_S = 0.5

type WSOrNone = web.WebSocketResponse | None


def _measured_only(
    values: dict[str, float], telemetry: TelemetrySupport
) -> dict[str, float | None]:
    return {
        "pos": values["pos"] if telemetry.position else None,
        "vel": values["vel"] if telemetry.velocity else None,
        "torque": values["torque"] if telemetry.current else None,
        "temp": values["temp"] if telemetry.temperature else None,
    }


def _level_for_state(state: BusHealth) -> str:
    if state is BusHealth.DOWN:
        return "critical"
    if state is BusHealth.DEGRADED:
        return "warning"
    return "info"


def _level_for_motor_state(state: MotorHealth) -> str:
    if state is MotorHealth.FAULT:
        return "critical"
    if state in (MotorHealth.STALE, MotorHealth.WARNING):
        return "warning"
    return "info"


@dataclass
class RobotContext:
    sequence: Sequence
    can_manager: CANManager
    position_loops: list[M3508PositionLoop] = field(default_factory=list)
    sync_monitors: list[SyncMonitor] = field(default_factory=list)
    target_refreshers: list[TargetRefresher] = field(default_factory=list)
    # リミットスイッチ保護。`limits:` を書いた軸が 1 本も無い構成では None。
    # 配信のためだけに持つ —— ラッチの解除は「センサが OFF になったこと」でしか
    # 起こしてはならず、押せるボタンを作れば端に触れたまま指令が通ってしまう
    limit_guard: LimitGuard | None = None
    # 手動操縦の指令口。位置定数を読めていないロボットでは None (手動不可)
    manual: ManualController | None = None
    mode: OperationMode = OperationMode.SEQUENCE


class RobotServer:
    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8080,
        *,
        health: HealthThresholds = DEFAULT_HEALTH,
        checklist_definitions: dict[str, list[ChecklistItem]] | None = None,
        match_settings: MatchSettings = DEFAULT_MATCH,
        dry_run: bool = False,
        dev_tools: bool = False,
    ) -> None:
        self._host = host
        self._port = port
        self._app: web.Application | None = None
        self._robots: dict[str, RobotContext] = {}
        self._ws = WsHub()
        self._broadcast_interval: float = 0.05
        self._broadcast_task: asyncio.Task[None] | None = None
        self._e_stop_active: bool = False
        self._e_stop_reason: str | None = None
        self._board_e_stop_ignore_before: float = 0.0
        self._energize_expected_since: float | None = None
        self._server_started_at: float | None = None
        self._inactive_motors: dict[str, list[str]] = {}
        self._reactivate_tasks: set[asyncio.Task[None]] = set()
        self._reenergize_tasks: dict[str, asyncio.Task[None]] = {}
        self._failed_tasks: dict[str, deque[str]] = {}
        self._dry_run: bool = dry_run
        self._dev_tools: bool = dev_tools
        self._sequence_tasks: dict[str, asyncio.Task[None]] = {}

        self.match = MatchState(definitions=checklist_definitions, settings=match_settings)

        self._health = health
        self._last_health: dict[str, HealthSnapshot] = {}

        self._motor_check = MotorCheckController(
            environment_deny=self._motor_check_environment_deny,
            pausables=self._motor_check_pausables,
            is_e_stop_active=lambda: self._e_stop_active,
            broadcast=self._ws.broadcast_json,
        )

        self._verify_command_handlers()

    def _verify_command_handlers(self) -> None:
        missing = [
            f"{spec.name} -> {spec.handler}"
            for spec in COMMANDS.values()
            if not callable(getattr(self, spec.handler, None))
        ]
        if missing:
            raise RuntimeError(
                "コマンド語彙が宣言したハンドラが実装されていません: " + ", ".join(missing)
            )

    @property
    def dev_tools(self) -> bool:
        return self._dev_tools

    @property
    def _reactivating(self) -> bool:
        return any(not task.done() for task in self._reactivate_tasks)

    @property
    def e_stop_active(self) -> bool:
        return self._e_stop_active

    @property
    def robot_names(self) -> tuple[str, ...]:
        return tuple(self._robots)

    def watch_task(
        self, task: asyncio.Task[None], *, context: str, robots: Collection[str]
    ) -> None:

        def _on_done(t: asyncio.Task[None]) -> None:
            if t.cancelled():
                return
            exc = t.exception()
            if exc is None:
                return
            logger.error("投げっぱなしタスクが失敗しました: %s", context, exc_info=exc)
            label = f"{context} ({type(exc).__name__})"
            for robot_name in robots:
                failures = self._failed_tasks.setdefault(
                    robot_name, deque(maxlen=_FAILED_TASK_BACKLOG)
                )
                if label not in failures:
                    failures.append(label)

        task.add_done_callback(_on_done)

    def add_robot(
        self,
        name: str,
        sequence: Sequence,
        can_manager: CANManager,
        position_loops: list[M3508PositionLoop] | None = None,
        sync_monitors: list[SyncMonitor] | None = None,
        target_refreshers: list[TargetRefresher] | None = None,
        limit_guard: LimitGuard | None = None,
        manual: ManualController | None = None,
    ) -> None:
        self._robots[name] = RobotContext(
            sequence=sequence,
            can_manager=can_manager,
            position_loops=list(position_loops or []),
            sync_monitors=list(sync_monitors or []),
            target_refreshers=list(target_refreshers or []),
            limit_guard=limit_guard,
            manual=manual,
        )
        sequence.set_court(self.match.court)
        if manual is not None:
            manual.set_court(self.match.court)

    def set_motor_check_sequence(self, sequence: Sequence) -> None:
        self._motor_check.set_sequence(sequence, court=self.match.court)

    def _apply_court(self) -> None:
        self._motor_check.set_court(self.match.court)
        for ctx in self._robots.values():
            ctx.sequence.set_court(self.match.court)
            if ctx.manual is not None:
                ctx.manual.set_court(self.match.court)

    def create_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/health", self._health_handler)
        app.router.add_get("/ws", self._ws_handler)
        app.router.add_post("/motor_check", self._motor_check_post)
        app.router.add_get("/motor_check", self._motor_check_get)

        if _WEB_DIST_DIR.is_dir():
            app.router.add_static("/assets", _WEB_DIST_DIR / "assets")
            app.router.add_get("/{path:.*}", self._spa_handler)

        app.on_startup.append(self._on_startup)
        app.on_shutdown.append(self._on_shutdown)

        self._app = app
        return app

    async def _spa_handler(self, request: web.Request) -> web.StreamResponse:
        path = request.match_info.get("path", "")
        file_path = _WEB_DIST_DIR / path
        if path and file_path.is_file():
            return web.FileResponse(file_path)
        return web.FileResponse(_WEB_DIST_DIR / "index.html")

    async def _health_handler(self, request: web.Request) -> web.Response:
        robots_payload: dict[str, dict] = {}
        overalls: list[BusHealth] = []
        for robot_name in self._robots:
            snap = self._compute_health(robot_name)
            robots_payload[robot_name] = snap.to_dict()
            overalls.append(snap.overall)

        overall = worst_bus_health(overalls)

        status = 200 if overall is BusHealth.OK else 503
        return web.json_response(
            {"overall": overall.value, "robots": robots_payload},
            status=status,
        )

    async def _on_startup(self, app: web.Application) -> None:
        self._energize_expected_since = time.time()
        self._server_started_at = time.time()
        self._broadcast_task = asyncio.create_task(self._broadcast_loop())
        for robot_name, ctx in self._robots.items():
            self._sequence_tasks[robot_name] = asyncio.create_task(ctx.sequence.run_forever())

    async def _on_shutdown(self, app: web.Application) -> None:
        if self._broadcast_task is not None:
            self._broadcast_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._broadcast_task

        for task in self._sequence_tasks.values():
            task.cancel()
        for task in self._sequence_tasks.values():
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._sequence_tasks.clear()

        for task in self._reactivate_tasks:
            task.cancel()
        self._reactivate_tasks.clear()
        for task in self._reenergize_tasks.values():
            task.cancel()
        self._reenergize_tasks.clear()

        self._ws.cancel_closing_tasks()
        await self._ws.close_all()

    async def _ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._ws.add(ws)
        logger.info("WebSocket 接続: %s", request.remote)

        for snapshot in (
            self._server_info_dict(),
            self.match.to_dict(),
            self._motor_check.payload(),
        ):
            if not await self._ws.send_or_drop(ws, json.dumps(snapshot, ensure_ascii=False)):
                await self._ws.drop({ws})
                return ws

        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except json.JSONDecodeError:
                        logger.warning("不正な JSON を受信: %s", msg.data)
                        continue
                    await self.handle_command(data, requester=ws)
                elif msg.type == WSMsgType.ERROR:
                    logger.error("WebSocket エラー: %s", ws.exception())
        finally:
            self._ws.discard(ws)
            logger.info("WebSocket 切断: %s", request.remote)

        return ws

    def _server_info_dict(self) -> dict:
        return {
            "type": "server_info",
            "dev_tools": self._dev_tools,
            "dry_run": self._dry_run,
            "temp_warning_c": self._health.temp_warning_c,
            "temp_critical_c": self._health.temp_critical_c,
        }

    async def handle_command(
        self,
        data: dict,
        *,
        requester: WSOrNone = None,
    ) -> None:
        spec = spec_for(data.get("type"))
        if spec is None:
            logger.debug("未知のコマンド: %s", data.get("type"))
            return

        deny = spec.dev_tools_deny_reason(self._dev_tools)
        if deny is None:
            deny = spec.phase_deny_reason(self.match.phase)
        if deny is None and self._e_stop_active:
            deny = spec.e_stop_deny_reason()
        if deny is None:
            deny = self._manual_mode_deny_reason(spec, data)
        if deny is None:
            deny = self._reenergize_deny_reason(spec, data)
        if deny is not None:
            logger.info("コマンド拒否: %s (%s)", spec.name, deny)
            await self._reject_by_channel(spec, data, requester, deny)
            return

        handler: Callable[[dict, WSOrNone], Awaitable[None]] = getattr(self, spec.handler)
        try:
            await handler(data, requester)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("コマンド処理に失敗: %s", spec.name)
            await self._reject_by_channel(
                spec, data, requester, f"コマンドの処理に失敗しました ({exc})"
            )

    async def _reject_by_channel(
        self,
        spec: CommandSpec,
        data: dict,
        requester: WSOrNone,
        reason: str,
    ) -> None:
        if spec.reject_channel is RejectChannel.MOTOR_CHECK_ERROR:
            await self._motor_check.report_error(reason)
        else:
            await self._reject_command(requester, spec.name, reason)

    def _manual_mode_deny_reason(self, spec: CommandSpec, data: dict) -> str | None:
        reason = spec.manual_deny_reason()
        if reason is None:
            return None
        robot_name = data.get("robot")
        if not isinstance(robot_name, str):
            return None
        ctx = self._robots.get(robot_name)
        if ctx is None or ctx.mode is not OperationMode.MANUAL:
            return None
        return reason

    def _reenergize_deny_reason(self, spec: CommandSpec, data: dict) -> str | None:
        reason = spec.reenergize_deny_reason()
        if reason is None:
            return None
        robot_name = data.get("robot")
        if not isinstance(robot_name, str) or robot_name not in self._robots:
            return None
        if not self._is_reenergizing(robot_name):
            return None
        return reason

    def _is_reenergizing(self, robot_name: str) -> bool:
        if self._reactivating:
            return True
        task = self._reenergize_tasks.get(robot_name)
        return task is not None and not task.done()

    async def _cmd_trigger(self, data: dict, _requester: WSOrNone) -> None:
        robot_name = data.get("robot")
        if robot_name and robot_name in self._robots:
            self._robots[robot_name].sequence.trigger()
            logger.info("trigger: %s", robot_name)

    async def _cmd_e_stop(self, _data: dict, _requester: WSOrNone) -> None:
        await self.activate_e_stop()

    async def _cmd_e_stop_release(self, _data: dict, requester: WSOrNone) -> None:
        if not self._e_stop_active:
            await self._reject_command(requester, "e_stop_release", "緊急停止中ではありません")
            return

        logger.info("緊急停止解除コマンド受信")
        self._reset_sync_latches()
        self._e_stop_active = False
        self._e_stop_reason = None
        await self._broadcast_e_stop_state()
        task = asyncio.create_task(self._reactivate_motors())
        self._reactivate_tasks.add(task)
        task.add_done_callback(self._reactivate_tasks.discard)
        self.watch_task(task, context="緊急停止解除の再励磁", robots=self.robot_names)

    async def _cmd_reenergize_motors(self, data: dict, requester: WSOrNone) -> None:
        robot_name = data.get("robot")
        if not isinstance(robot_name, str) or robot_name not in self._robots:
            return

        if self._motor_check.running:
            await self._reject_command(
                requester, "reenergize_motors", "動作確認の実行中は再励磁できません"
            )
            return
        if self._reactivating:
            await self._reject_command(
                requester, "reenergize_motors", "緊急停止解除の再励磁が進行中です"
            )
            return
        if self._is_reenergizing(robot_name):
            await self._reject_command(requester, "reenergize_motors", "再励磁の処理中です")
            return

        logger.info("再励磁コマンド受信: robot=%s", robot_name)
        task = asyncio.create_task(self._reenergize_motors(robot_name))
        self._reenergize_tasks[robot_name] = task
        self.watch_task(task, context="再励磁", robots=[robot_name])
        task.add_done_callback(
            lambda t, name=robot_name: (
                self._reenergize_tasks.pop(name, None)
                if self._reenergize_tasks.get(name) is t
                else None
            )
        )

    async def _cmd_health_check(self, _data: dict, _requester: WSOrNone) -> None:
        await self._broadcast_state()

    async def _cmd_sequence_jump(self, data: dict, _requester: WSOrNone) -> None:
        robot_name = data.get("robot")
        step_index = data.get("step_index")
        if isinstance(step_index, bool) or not isinstance(step_index, int):
            return
        if robot_name and robot_name in self._robots:
            self._robots[robot_name].sequence.request_jump(step_index)
            logger.info("sequence_jump: %s -> %d", robot_name, step_index)

    async def _cmd_sequence_stop(self, data: dict, _requester: WSOrNone) -> None:
        robot_name = data.get("robot")
        if robot_name and robot_name in self._robots:
            self._stop_sequence(self._robots[robot_name])
            logger.info("sequence_stop: %s", robot_name)

    async def _cmd_sequence_start(self, data: dict, _requester: WSOrNone) -> None:
        robot_name = data.get("robot")
        if robot_name and robot_name in self._robots:
            self._robots[robot_name].sequence.request_start()
            logger.info("sequence_start: %s", robot_name)

    async def _cmd_motor_check_start(self, _data: dict, _requester: WSOrNone) -> None:
        await self._motor_check.start()

    async def _cmd_motor_check_abort(self, _data: dict, _requester: WSOrNone) -> None:
        self._motor_check.abort()

    async def _cmd_set_operation_mode(self, data: dict, requester: WSOrNone) -> None:
        robot_name = data.get("robot")
        if not isinstance(robot_name, str) or robot_name not in self._robots:
            return
        try:
            mode = OperationMode(data.get("mode"))
        except ValueError:
            await self._reject_command(
                requester, "set_operation_mode", f"未知の操作モード: {data.get('mode')!r}"
            )
            return
        await self._apply_operation_mode(robot_name, mode, requester=requester)

    async def _cmd_manual_move(self, data: dict, requester: WSOrNone) -> None:
        target = await self._manual_target(data, "manual_move", requester)
        if target is None:
            return
        manual, axis = target
        position = data.get("position")
        if not isinstance(position, str) or not position:
            await self._reject_command(requester, "manual_move", "位置名が指定されていません")
            return
        await self._run_manual("manual_move", requester, manual.move_to_position(axis, position))

    async def _cmd_manual_set(self, data: dict, requester: WSOrNone) -> None:
        target = await self._manual_target(data, "manual_set", requester)
        if target is None:
            return
        manual, axis = target
        value = await self._manual_number(data, "value", "manual_set", requester)
        if value is None:
            return
        await self._run_manual("manual_set", requester, manual.set_value(axis, value))

    async def _cmd_manual_jog(self, data: dict, requester: WSOrNone) -> None:
        target = await self._manual_target(data, "manual_jog", requester)
        if target is None:
            return
        manual, axis = target
        delta = await self._manual_number(data, "delta", "manual_jog", requester)
        if delta is None:
            return
        await self._run_manual("manual_jog", requester, manual.jog(axis, delta))

    async def _apply_operation_mode(
        self,
        robot_name: str,
        mode: OperationMode,
        *,
        requester: WSOrNone = None,
    ) -> bool:
        ctx = self._robots[robot_name]
        if ctx.mode is mode:
            return True

        if mode is OperationMode.MANUAL:
            if ctx.manual is None:
                await self._reject_command(
                    requester,
                    "set_operation_mode",
                    f"'{robot_name}' は手動操縦に対応していません (位置定数が未読込)",
                )
                return False
            if self._motor_check.running:
                await self._reject_command(
                    requester,
                    "set_operation_mode",
                    "動作確認の実行中は手動操縦へ切り替えられません",
                )
                return False
            if self._is_reenergizing(robot_name):
                await self._reject_command(
                    requester, "set_operation_mode", f"'{robot_name}' の再励磁が処理中です"
                )
                return False
            self._stop_sequence(ctx)

        ctx.mode = mode
        if ctx.manual is not None:
            ctx.manual.reset()
        logger.info("操作モード変更: robot=%s mode=%s", robot_name, mode.value)
        return True

    async def _manual_target(
        self,
        data: dict,
        command: str,
        requester: WSOrNone,
    ) -> tuple[ManualController, str] | None:
        robot_name = data.get("robot")
        if not isinstance(robot_name, str) or robot_name not in self._robots:
            return None

        ctx = self._robots[robot_name]
        if ctx.manual is None:
            await self._reject_command(
                requester, command, f"'{robot_name}' は手動操縦に対応していません"
            )
            return None
        if self._is_reenergizing(robot_name):
            await self._reject_command(requester, command, f"'{robot_name}' の再励磁が処理中です")
            return None

        axis = data.get("axis")
        if not isinstance(axis, str) or not axis:
            await self._reject_command(requester, command, "軸が指定されていません")
            return None

        # モード判定は軸の解決より後。manual_always の軸はシーケンス制御中でも通すので、
        # どちらの軸かが分かるまで可否を決められない
        if ctx.mode is not OperationMode.MANUAL and not await self._allow_manual_in_sequence(
            ctx.manual, axis, command, requester
        ):
            return None
        return ctx.manual, axis

    async def _allow_manual_in_sequence(
        self,
        manual: ManualController,
        axis: str,
        command: str,
        requester: WSOrNone,
    ) -> bool:
        try:
            always_manual = manual.is_always_manual(axis)
        except ManualControlError as exc:
            await self._reject_command(requester, command, str(exc))
            return False

        if not always_manual:
            allowed = ", ".join(manual.always_manual_axes()) or "(なし)"
            await self._reject_command(
                requester,
                command,
                "手動操縦モードではありません (モードを切り替えてください)。"
                f"シーケンス制御中でも操作できる軸: {allowed}",
            )
            return False

        # 動作確認は conveyor / valve をまさにこの軸で駆動し、操縦者が目視・打音で確かめる。
        # 排他は「sequence モードでは手動が全部拒否される」に乗っていたので、軸単位で
        # 緩めたぶんをここで塞ぎ直す (`_motor_check_environment_deny` と対になる)
        if self._motor_check.running:
            await self._reject_command(requester, command, "動作確認の実行中は手動で操作できません")
            return False
        return True

    async def _manual_number(
        self,
        data: dict,
        key: str,
        command: str,
        requester: WSOrNone,
    ) -> float | None:
        value = data.get(key)
        if isinstance(value, bool) or not isinstance(value, int | float):
            await self._reject_command(requester, command, f"{key} が数値ではありません: {value!r}")
            return None
        if not math.isfinite(value):
            await self._reject_command(
                requester, command, f"{key} が有限な数値ではありません: {value!r}"
            )
            return None
        return float(value)

    async def _run_manual(
        self,
        command: str,
        requester: WSOrNone,
        coro: Awaitable[float],
    ) -> None:
        try:
            await coro
        except ManualControlError as exc:
            await self._reject_command(requester, command, str(exc))

    async def _cmd_set_court(self, data: dict, requester: WSOrNone) -> None:
        await self._handle_set_court(data, requester)

    async def _cmd_checklist_set(self, data: dict, _requester: WSOrNone) -> None:
        role = data.get("role")
        item_id = data.get("item_id")
        checked = bool(data.get("checked"))
        if isinstance(role, str) and isinstance(item_id, str):
            if self.match.set_checklist_item(role, item_id, checked):
                await self._broadcast_match_state()
            else:
                logger.warning("未知のチェック項目: role=%s item=%s", role, item_id)

    async def _cmd_checklist_check_all(self, data: dict, _requester: WSOrNone) -> None:
        role = data.get("role")
        self.match.check_all_checklist_items(role if isinstance(role, str) else None)
        logger.warning("開発用: 指差喚呼を一括チェックしました (role=%s)", role or "all")
        await self._broadcast_match_state()

    async def _cmd_checklist_reset(self, data: dict, _requester: WSOrNone) -> None:
        role = data.get("role")
        self.match.reset_checklist(role if isinstance(role, str) else None)
        await self._broadcast_match_state()

    async def _cmd_match_start(self, _data: dict, requester: WSOrNone) -> None:
        await self._handle_match_start(requester)

    async def _cmd_match_finish(self, _data: dict, _requester: WSOrNone) -> None:
        if self.match.match_finish():
            logger.info("試合終了")
            self._stop_all_sequences()
            for ctx in self._robots.values():
                for task in self._periodic_tasks(ctx):
                    task.log_jitter_summary()
                    task.reset_jitter_stats()
            await self._broadcast_match_state()

    async def _cmd_match_reset(self, _data: dict, requester: WSOrNone) -> None:
        self.match.match_reset()
        logger.info("セッティングタイムへ復帰")
        self._stop_all_sequences()
        for robot_name in self._robots:
            await self._apply_operation_mode(
                robot_name, OperationMode.SEQUENCE, requester=requester
            )
        self._apply_court()
        await self._broadcast_match_state()

    def _motor_command_state(self, robot_name: str, motor_name: str) -> dict[str, object]:
        sequence = self._robots[robot_name].sequence
        if not sequence.has_motors:
            return {"command": None, "command_mode": None}

        group = sequence.motors
        if motor_name not in group:
            return {"command": None, "command_mode": None}

        handle = group[motor_name]
        mode = handle.mode
        return {"command": handle.target, "command_mode": None if mode is None else mode.value}

    async def _handle_set_court(
        self,
        data: dict,
        requester: WSOrNone = None,
    ) -> None:
        raw = data.get("court")
        try:
            court = Court(raw)
        except ValueError:
            logger.warning("未知のコート: %s", raw)
            valid = "/".join(c.value for c in Court)
            await self._reject_command(
                requester, "set_court", f"未知のコートです: {raw!r} (有効: {valid})"
            )
            return
        if not self.match.set_court(court):
            return
        self._apply_court()
        logger.info("コート変更: %s", court.value)
        await self._broadcast_match_state()

    @staticmethod
    def _periodic_tasks(ctx: RobotContext) -> tuple[PeriodicTask, ...]:
        # 並べる箇所が match_start / match_finish の 2 つあるので 1 つにまとめてある。
        # 周期タスクを足したときは、ここのタプルと「持つとき / 持たないとき」を固定した
        # テストの両方を見ること。limit_guard は limits: を書いた軸が 1 本も無い
        # ロボットでは None (組み立てられない) なので、除いてから展開する
        return (
            *ctx.position_loops,
            *ctx.sync_monitors,
            *ctx.target_refreshers,
            *((ctx.limit_guard,) if ctx.limit_guard is not None else ()),
        )

    async def _handle_match_start(self, requester: WSOrNone = None) -> None:
        if self._motor_check.running:
            await self._reject_command(
                requester, "match_start", "動作確認の実行中は試合を開始できません"
            )
            return

        if not self.match.match_start():
            await self._reject_command(
                requester,
                "match_start",
                COMMANDS["match_start"].phase_deny_reason(self.match.phase)
                or "試合を開始できません",
            )
            return

        self._apply_court()

        for ctx in self._robots.values():
            ctx.can_manager.reset_rx_down_episodes()
            for task in self._periodic_tasks(ctx):
                task.reset_jitter_stats()

        self._failed_tasks.clear()

        logger.info("試合開始: court=%s", self.match.court.value)

        await self._broadcast_match_state()

    async def activate_e_stop(self, *, reason: str | None = None) -> None:
        logger.warning("緊急停止発動: %s", reason or "操縦者コマンド")
        self._e_stop_active = True
        if self._e_stop_reason is None:
            self._e_stop_reason = reason
        self._motor_check.abort()
        for ctx in self._robots.values():
            if ctx.manual is not None:
                ctx.manual.on_e_stop()
        try:
            await self._send_e_stop_frames()
        except Exception:
            logger.exception("E-STOP 停止フレーム送信に失敗")
        finally:
            self._stop_all_sequences()
            await self._broadcast_e_stop_state()

    async def _send_e_stop_frames(self) -> None:
        e_stop_msg = GenericDriver.encode_e_stop()
        for name, ctx in self._robots.items():
            for loop in ctx.position_loops:
                try:
                    await loop.send_stop_frame()
                except Exception:
                    logger.exception(
                        "E-STOP M3508 停止フレーム送信失敗: robot=%s bus=%s",
                        name,
                        loop.bus_name,
                    )
            for motor_name, motor in ctx.can_manager.motors.items():
                driver_stop = motor.emergency_stop_message()
                if driver_stop is None:
                    continue
                try:
                    await ctx.can_manager.send(motor_name, driver_stop)
                except Exception:
                    logger.exception(
                        "E-STOP driver固有送信失敗: robot=%s motor=%s",
                        name,
                        motor_name,
                    )
            for bus_name in ctx.can_manager.bus_names:
                try:
                    await ctx.can_manager.send_to_bus(bus_name, e_stop_msg)
                except Exception:
                    logger.exception(
                        "E-STOP bus送信失敗: robot=%s bus=%s",
                        name,
                        bus_name,
                    )
            for refresher in ctx.target_refreshers:
                refresher.clear_targets()
        logger.info("E-STOP 送信試行完了: %s", ", ".join(self._robots) or "対象なし")

    async def _send_e_stop_clear_broadcast(self) -> None:
        clear_msg = GenericDriver.encode_e_stop_clear()
        for name, ctx in self._robots.items():
            for bus_name in ctx.can_manager.bus_names:
                try:
                    await ctx.can_manager.send_to_bus(bus_name, clear_msg)
                except Exception:
                    logger.exception(
                        "E-STOP 解除ブロードキャスト送信失敗: robot=%s bus=%s",
                        name,
                        bus_name,
                    )

    def _reset_sync_latches(self) -> None:
        """同期ずれのラッチを解除し、監視を再び有効な状態へ戻す。

        リミット保護のラッチ (`LimitGuard.reset()`) はここへ相乗りさせない ——
        まだ触れていれば次の周期で再ラッチし、離れていれば自動解除で外れているので、
        呼んでも結果が変わらないまま「緊急停止の解除でリミットのラッチも外れる」という
        効いていない因果をコードが主張することになる。
        """
        for name, ctx in self._robots.items():
            for loop in ctx.position_loops:
                loop.reset_sync_violation()
            for monitor in ctx.sync_monitors:
                monitor.reset()
            logger.info("同期ずれラッチを解除: robot=%s", name)

    def _safety_state(self, robot_name: str) -> dict[str, object]:
        ctx = self._robots[robot_name]
        violations: set[str] = set()
        for loop in ctx.position_loops:
            violations |= set(loop.sync_violations)
        for monitor in ctx.sync_monitors:
            violations |= set(monitor.violated)

        return {
            "sync_violations": sorted(violations),
            "unenergized_motors": self._unenergized_motors(robot_name),
            "firmware_unconfirmed_motors": self._firmware_unconfirmed_motors(robot_name),
            # リミット保護が今止めている軸 (軸名 → ラッチ中のセンサ名)。配らないと
            # 操縦者には「その向きへ指令しても動かない」以外の手掛かりが 1 つも無い。
            # 介入回数は配信しない (手動で端へ寄せるたびに増え、シーケンス由来と
            # 混ざった数になる。あちらは move_to が到達を疑うための内部の口)
            "limit_latched": self._limit_latched(robot_name),
            # フィードバック途絶で保護が効いていないセンサ。「壊れている」ではなく
            # 「保護が働いていない」の報告なので FAULT へ倒さない
            "limit_blind_sensors": self._limit_blind_sensors(robot_name),
            # 投げっぱなしタスク (`watch_task`) が拾った失敗。平常時は空配列。
            # 古い順に並ぶ (`_FAILED_TASK_BACKLOG` を超えた分は古いものから消える)。
            # 復帰しても消えない —— リセットは `_handle_match_start` の前縁リセットだけ
            "failed_tasks": list(self._failed_tasks.get(robot_name, ())),
            "reenergizing": self._is_reenergizing(robot_name),
            "loops_running": all(loop.is_running for loop in ctx.position_loops),
            "monitors_running": all(monitor.is_running for monitor in ctx.sync_monitors),
            "refreshers_running": all(r.is_running for r in ctx.target_refreshers),
            "position_loops": [
                {
                    "bus": loop.bus_name,
                    "running": loop.is_running,
                    "paused": loop.is_paused,
                    "sync_violations": sorted(loop.sync_violations),
                }
                for loop in ctx.position_loops
            ],
            "sync_monitors": [
                {
                    "axes": list(monitor.group_names),
                    "running": monitor.is_running,
                    "violated": sorted(monitor.violated),
                }
                for monitor in ctx.sync_monitors
            ],
            "target_refreshers": [
                {
                    "motors": list(refresher.motor_names),
                    "running": refresher.is_running,
                    "paused": refresher.is_paused,
                }
                for refresher in ctx.target_refreshers
            ],
        }

    def _unenergized_motors(self, robot_name: str) -> list[str]:
        since = self._energize_expected_since
        if self._e_stop_active or since is None:
            return []
        if time.time() - since < _ENERGIZE_GRACE_S:
            return []

        ctx = self._robots[robot_name]
        names = {
            motor_name
            for motor_name, motor in ctx.can_manager.motors.items()
            if motor.is_energized() is False
        }
        names.update(self._inactive_motors.get(robot_name, ()))
        return sorted(names)

    def _firmware_unconfirmed_motors(self, robot_name: str) -> list[str]:
        if self._dry_run:
            return []

        since = self._server_started_at
        if since is None or time.time() - since < _FIRMWARE_INFO_GRACE_S:
            return []

        ctx = self._robots[robot_name]
        freshness = FeedbackFreshness(
            ctx.can_manager.last_feedback_at, timeout_ms=self._health.feedback_timeout_ms
        )
        now = freshness.now()
        devices = {**ctx.can_manager.motors, **ctx.can_manager.sensors}
        return sorted(
            device_name
            for device_name, device in devices.items()
            if device.firmware_confirmed() is False and not freshness.is_stale(device_name, now)
        )

    def _limit_latched(self, robot_name: str) -> dict[str, list[str]]:
        """リミットスイッチ保護が今止めている軸 → ラッチ中のセンサ名。平常時は空。

        **黙って止まる軸を作らないための唯一の報告経路である。** 保護は軸ローカルで
        全体緊急停止に倒さない (端に触れた軸を戻す操作そのものを塞がないため) ので、
        操縦者から見えるのは「その向きへ指令しても機体が動かない」だけになる。
        シーケンスは `SequenceTimeoutError` で止まるが、手動操縦には止まる理由が
        1 つも現れない。

        **センサ名まで配る。** どちらの端に触れているかはセンサ名にしか無く、
        軸名だけでは退避の向きを選べない (両端にスイッチのある `sub_y_axis` /
        `sub_lift` では 2 通りある)。UI 側に軸名・センサ名の表を持たせないため、
        `LimitGuard` が持つ名前をそのまま流す。

        **回数 (`LimitGuard.intervention`) は載せない。** あちらは手動で端へ寄せる
        たびに増えるので、シーケンス由来と手動由来が混ざった数になり操縦者には
        読めない (`move_to` が到達を疑うための内部の口である)。
        """
        guard = self._robots[robot_name].limit_guard
        if guard is None:
            return {}
        return {axis: list(sensors) for axis, sensors in guard.latched.items()}

    def _limit_blind_sensors(self, robot_name: str) -> list[str]:
        """フィードバック途絶でリミット保護が効いていないセンサ。平常時は空。

        **これは「壊れている」ではなく「保護が働いていない」の報告である**
        (`_firmware_unconfirmed_motors` と同じ位置付け)。途絶で軸を止めると
        スイッチ 1 本の不調で試合中に機体が動かなくなるので `LimitGuard` は
        判定しない方を選んでおり、そのぶん**効いていないことは必ず見えなければ
        ならない** —— 黙って無効になれば「守っているつもり」の機体ができる。

        FAULT にも `evaluateHealth` の判定にも倒さない。センサの途絶そのものは
        既に `MotorHealth.STALE` として別に主張しており、ここで足すのは
        「そのぶん保護が消えている」という別の事実だけである。

        **dry-run は対象外。** virtual バスはセンサの `FEEDBACK` を 1 通も返さない
        ので、机上では全センサが恒久的にここへ並び、画面を確かめられなくなる
        (`_firmware_unconfirmed_motors` と同じ理由)。
        """
        if self._dry_run:
            return []
        guard = self._robots[robot_name].limit_guard
        return list(guard.blind_sensors) if guard is not None else []

    async def _reactivate_motors(self) -> None:
        await self._settle_pending_reactivation()
        await self._send_e_stop_clear_broadcast()

        for name, ctx in self._robots.items():
            try:
                uncleared = await ctx.can_manager.clear_e_stop_latches()
            except Exception:
                logger.exception("緊急停止ラッチの解除に失敗: robot=%s", name)
                uncleared = list(ctx.can_manager.motors)
            if uncleared:
                logger.error(
                    "緊急停止ラッチを解除できなかったモータ: robot=%s motors=%s",
                    name,
                    ", ".join(uncleared),
                )

        for name, ctx in self._robots.items():
            await self._settle_pending_reenergize(name)
            await self._activate_motors_for_robot(name, ctx)

        self._energize_expected_since = time.time()

        self._board_e_stop_ignore_before = time.time()

        if self._e_stop_active:
            logger.warning("有効化中に緊急停止が再度入ったため停止フレームを再送します")
            try:
                await self._send_e_stop_frames()
            except Exception:
                logger.exception("E-STOP 停止フレーム再送に失敗")

    async def _settle_pending_reenergize(self, robot_name: str) -> None:
        pending = self._reenergize_tasks.get(robot_name)
        if pending is None or pending.done():
            return
        pending.cancel()
        # `asyncio.wait` は中の例外 (CancelledError を含む) を送出しない。
        done, _still_running = await asyncio.wait({pending}, timeout=_PENDING_TASK_CANCEL_TIMEOUT_S)
        if not done:
            logger.error(
                "再励磁タスクが %.1fs 以内に畳めませんでした: robot=%s"
                " (CAN の送信が詰まっている可能性があります)。解除の励磁を先へ進めます",
                _PENDING_TASK_CANCEL_TIMEOUT_S,
                robot_name,
            )

    async def _settle_pending_reactivation(self) -> None:
        current = asyncio.current_task()
        pending = {
            task for task in self._reactivate_tasks if not task.done() and task is not current
        }
        if not pending:
            return
        for task in pending:
            task.cancel()
        done, _still_running = await asyncio.wait(pending, timeout=_PENDING_TASK_CANCEL_TIMEOUT_S)
        if len(done) != len(pending):
            logger.error(
                "解除の再励磁タスクが %.1fs 以内に畳めませんでした"
                " (CAN の送信が詰まっている可能性があります)。新しい解除を先へ進めます",
                _PENDING_TASK_CANCEL_TIMEOUT_S,
            )

    async def _activate_motors_for_robot(
        self, robot_name: str, ctx: RobotContext, *, only: Collection[str] | None = None
    ) -> list[str]:
        try:
            inactive = await ctx.can_manager.activate_motors(
                should_abort=lambda: self._e_stop_active, only=only
            )
        except Exception:
            logger.exception("モータ有効化に失敗: robot=%s", robot_name)
            inactive = list(only) if only is not None else list(ctx.can_manager.motors)
        if inactive:
            logger.error(
                "有効化後も無励磁のまま残ったモータ: robot=%s motors=%s",
                robot_name,
                ", ".join(inactive),
            )
        self._inactive_motors[robot_name] = list(inactive)
        return inactive

    async def _reenergize_motors(self, robot_name: str) -> None:
        try:
            ctx = self._robots[robot_name]
            dropped = {
                name
                for name, motor in ctx.can_manager.motors.items()
                if motor.is_energized() is False
            } | set(self._inactive_motors.get(robot_name, ()))
            for monitor in ctx.sync_monitors:
                for group in monitor.groups:
                    member_names = {member.name for member in group.members}
                    if dropped & member_names:
                        dropped |= member_names
            if not dropped:
                logger.info("再励磁の対象モータがありません: robot=%s", robot_name)
                return

            for refresher in ctx.target_refreshers:
                for name in dropped.intersection(refresher.motor_names):
                    refresher.clear_target(name)

            if ctx.manual is not None:
                ctx.manual.reset_axes_for_motors(dropped)

            await self._activate_motors_for_robot(robot_name, ctx, only=dropped)
            self._energize_expected_since = time.time()
        except Exception:
            logger.exception("再励磁処理で予期しない例外: robot=%s", robot_name)
            return

        if self._e_stop_active:
            logger.warning("再励磁中に緊急停止が入ったため停止フレームを再送します")
            try:
                await self._send_e_stop_frames()
            except Exception:
                logger.exception("E-STOP 停止フレーム再送に失敗")

    def _stop_all_sequences(self) -> None:
        for ctx in self._robots.values():
            self._stop_sequence(ctx)

    def _stop_sequence(self, ctx: RobotContext) -> None:
        ctx.sequence.discard_pending_start()
        if ctx.sequence.is_running:
            ctx.sequence.request_stop()

    def set_initial_inactive_motors(self, robot_name: str, motor_names: list[str]) -> None:
        if motor_names:
            logger.error(
                "起動時に励磁できなかったモータ: robot=%s motors=%s",
                robot_name,
                ", ".join(motor_names),
            )
        self._inactive_motors[robot_name] = list(motor_names)

    async def _broadcast_match_state(self) -> None:
        await self._ws.broadcast_json(self.match.to_dict())

    async def _reject_command(
        self,
        requester: WSOrNone,
        command: str,
        reason: str,
    ) -> None:
        if requester is None or requester.closed:
            return
        msg = json.dumps(
            {"type": "command_rejected", "command": command, "reason": reason},
            ensure_ascii=False,
        )
        if not await self._ws.send_or_drop(requester, msg):
            await self._ws.drop({requester})

    async def _broadcast_e_stop_state(self) -> None:
        payload: dict[str, object] = {"type": "e_stop_state", "active": self._e_stop_active}
        if self._e_stop_reason is not None:
            payload["reason"] = self._e_stop_reason
        await self._ws.broadcast_json(payload)

    def _motor_check_environment_deny(self) -> str | None:
        phase_deny = COMMANDS["motor_check_start"].phase_deny_reason(self.match.phase)
        if phase_deny is not None:
            return phase_deny

        if self._e_stop_active:
            return "緊急停止中のため動作確認を実行できません"

        for name in self._robots:
            if self._is_reenergizing(name):
                return f"'{name}' の再励磁が完了していないため動作確認を実行できません"

        for name, ctx in self._robots.items():
            if ctx.mode is OperationMode.MANUAL:
                return f"'{name}' が手動操縦モードのため動作確認を実行できません"
        for name, ctx in self._robots.items():
            if ctx.sequence.is_running:
                return f"'{name}' の通常シーケンス実行中のため動作確認を実行できません"
        return None

    def _motor_check_pausables(self) -> list[Pausable]:
        return []

    async def _motor_check_post(self, request: web.Request) -> web.Response:
        started = await self._motor_check.start()
        if not started:
            return web.json_response(
                {"started": False, "reason": self._motor_check.error}, status=409
            )
        return web.json_response({"started": True}, status=200)

    async def _motor_check_get(self, request: web.Request) -> web.Response:
        return web.json_response(self._motor_check.payload(), status=200)

    async def _broadcast_loop(self) -> None:
        while True:
            try:
                await self._broadcast_state()
                await self._motor_check.publish()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("状態配信に失敗しました (配信は継続します)")
            await asyncio.sleep(self._broadcast_interval)

    def _compute_health(self, robot_name: str) -> HealthSnapshot:
        ctx = self._robots[robot_name]
        try:
            snap = ctx.can_manager.health(thresholds=self._health)
        except Exception as exc:
            logger.exception("ヘルス計算に失敗: robot=%s", robot_name)
            return self._health_unknown(f"ヘルス計算に失敗しました: {exc}")

        if not isinstance(snap, HealthSnapshot):
            logger.error(
                "ヘルス計算が HealthSnapshot 以外を返しました: robot=%s type=%s",
                robot_name,
                type(snap).__name__,
            )
            return self._health_unknown(
                f"ヘルス計算が不正な戻り値を返しました ({type(snap).__name__})"
            )
        return snap

    def _health_unknown(self, detail: str) -> HealthSnapshot:
        return HealthSnapshot(timestamp=time.time(), overall=BusHealth.DOWN, detail=detail)

    def _diff_health(
        self,
        robot_name: str,
        prev: HealthSnapshot | None,
        curr: HealthSnapshot,
    ) -> list[dict]:
        if prev is None:
            return []

        events: list[dict] = []

        prev_buses: dict[str, BusHealthInfo] = {b.name: b for b in prev.buses}
        for b in curr.buses:
            old = prev_buses.get(b.name)
            if old is not None and old.state is not b.state:
                events.append(
                    {
                        "type": "health_change",
                        "robot": robot_name,
                        "level": _level_for_state(b.state),
                        "target": f"bus:{b.name}",
                        "from": old.state.value,
                        "to": b.state.value,
                        "message": f"{b.channel or b.name} {old.state.value}→{b.state.value}",
                    }
                )

        prev_motors: dict[str, MotorHealthInfo] = {m.name: m for m in prev.motors}
        for m in curr.motors:
            old_m = prev_motors.get(m.name)
            if old_m is not None and old_m.state is not m.state:
                events.append(
                    {
                        "type": "health_change",
                        "robot": robot_name,
                        "level": _level_for_motor_state(m.state),
                        "target": f"motor:{m.name}",
                        "from": old_m.state.value,
                        "to": m.state.value,
                        "message": f"motor {m.name} {old_m.state.value}→{m.state.value}",
                    }
                )

        return events

    def _detect_board_e_stop(self, snapshots: dict[str, HealthSnapshot]) -> str | None:
        if self._e_stop_active:
            return None

        if self._reactivating:
            return None

        for robot_name, ctx in self._robots.items():
            snapshot = snapshots.get(robot_name)
            if snapshot is None:
                continue
            last_feedback = {info.name: info.last_feedback_at for info in snapshot.motors}
            for motor_name, motor in ctx.can_manager.motors.items():
                if not isinstance(motor, GenericDriver) or not motor.e_stop_active:
                    continue
                received_at = last_feedback.get(motor_name)
                if received_at is None or received_at <= self._board_e_stop_ignore_before:
                    continue
                return (
                    f"{robot_name} の {motor_name} が基板側の緊急停止を報告 "
                    "(物理停止スイッチ / CAN 不通)"
                )
        return None

    async def _broadcast_state(self) -> None:
        snapshots: dict[str, HealthSnapshot] = {}
        for robot_name in self._robots:
            snapshots[robot_name] = self._compute_health(robot_name)

        board_e_stop = self._detect_board_e_stop(snapshots)
        if board_e_stop is not None:
            await self.activate_e_stop(reason=board_e_stop)

        if not self._ws.has_clients:
            self._last_health = snapshots
            return

        state_messages: list[dict] = []
        change_events: list[dict] = []
        for robot_name, snap in snapshots.items():
            state_messages.append(self._build_state_message(robot_name, snapshot=snap))

            prev = self._last_health.get(robot_name)
            change_events.extend(self._diff_health(robot_name, prev, snap))

        await self._ws.fanout([*state_messages, *change_events])

        self._last_health = snapshots

        if self._e_stop_active:
            await self._broadcast_e_stop_state()

    def _build_state_message(
        self,
        robot_name: str,
        *,
        snapshot: HealthSnapshot | None = None,
    ) -> dict:
        ctx = self._robots[robot_name]
        progress = ctx.sequence.progress

        motors: dict[str, dict] = {}
        for motor_name, motor in ctx.can_manager.motors.items():
            if self._dry_run:
                raw = server_dryrun.motor_state(robot_name, motor_name)
            else:
                s = motor.state
                raw = {
                    "pos": s.position,
                    "vel": s.velocity,
                    "torque": s.current,
                    "temp": s.temperature,
                }
            motors[motor_name] = _measured_only(raw, motor.telemetry)
            motors[motor_name].update(self._motor_command_state(robot_name, motor_name))

        if snapshot is None:
            snapshot = self._compute_health(robot_name)

        snapshot_dict = snapshot.to_dict()
        if self._dry_run:
            snapshot_dict = server_dryrun.patch_health(snapshot_dict)

        return {
            "type": "state",
            "robot": robot_name,
            "sequence": progress["sequence"],
            "current_step": progress["current_step"],
            "step_index": progress["step_index"],
            "total_steps": progress["total_steps"],
            "waiting_trigger": progress["waiting_trigger"],
            "running": progress["running"],
            "steps": progress.get("steps", []),
            "last_error": progress["last_error"],
            "motors": motors,
            "sensors": self._sensor_states(robot_name),
            "e_stop_active": self.e_stop_active,
            "health": snapshot_dict,
            "safety": self._safety_state(robot_name),
            "manual": self._manual_state(robot_name),
        }

    def _sensor_states(self, robot_name: str) -> dict[str, dict]:
        ctx = self._robots[robot_name]
        freshness = FeedbackFreshness(
            ctx.can_manager.last_feedback_at, timeout_ms=self._health.feedback_timeout_ms
        )
        now = freshness.now()

        sensors: dict[str, dict] = {}
        for sensor_name, sensor in ctx.can_manager.sensors.items():
            if self._dry_run:
                sensors[sensor_name] = server_dryrun.sensor_state(robot_name, sensor_name)
                continue
            sensors[sensor_name] = {
                "active": sensor.sensor_active if isinstance(sensor, GenericDriver) else None,
                "stale": freshness.is_stale(sensor_name, now),
            }
        return sensors

    def _manual_state(self, robot_name: str) -> dict:
        ctx = self._robots[robot_name]
        return {
            "mode": ctx.mode.value,
            "axes": ctx.manual.axes_info() if ctx.manual is not None else [],
        }

    async def start(self) -> None:
        app = self.create_app()
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, self._host, self._port)
        await site.start()
        logger.info(
            "サーバー起動: http://%s:%d%s",
            self._host,
            self._port,
            " (dry-run)" if self._dry_run else "",
        )

        try:
            await asyncio.Event().wait()
        finally:
            await runner.cleanup()

    async def cleanup(self) -> None:
        await self._ws.close_all()
