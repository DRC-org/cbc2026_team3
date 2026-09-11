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
from lib.control.limit_monitor import LimitMonitor
from lib.control.periodic import PeriodicTask
from lib.control.position_loop import M3508PositionLoop
from lib.control.sync_monitor import SyncMonitor
from lib.control.target_refresh import TargetRefresher, hold_target_refresh
from lib.drivers.base import ControlMode, TelemetrySupport
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
from lib.match_state import Court, MatchState
from lib.motion_guard import GuardViolation
from lib.position_capture import PositionCaptureStore
from lib.sequence.engine import Sequence
from lib.sequence.positions import (
    CourtUnresolvedError,
    PositionLookupError,
    PositionReloadError,
)
from lib.server_homing import HomingController, HomingSource
from lib.server_motor_check import MotorCheckController, Pausable
from lib.server_switch_measure import SwitchMeasureController
from lib.suction import SuctionSelection
from lib.ws_hub import WsHub
from lib.ws_state import StateThinner

logger = logging.getLogger(__name__)

_WEB_DIST_DIR = pathlib.Path(__file__).resolve().parent.parent / "web" / "dist"


_FAILED_TASK_BACKLOG = 5

_ENERGIZE_GRACE_S = 0.5

_FIRMWARE_INFO_GRACE_S = 3.0

_PENDING_TASK_CANCEL_TIMEOUT_S = 0.5

# 配信が遅れているときに空ける上限。これ以上空けると画面の値が古すぎて操縦に使えない
_BROADCAST_MAX_INTERVAL_S = 0.5

type WSOrNone = web.WebSocketResponse | None


def _measured_only(
    values: dict[str, float], telemetry: TelemetrySupport
) -> dict[str, float | None]:
    return {
        "pos": _display(values["pos"]) if telemetry.position else None,
        "vel": _display(values["vel"]) if telemetry.velocity else None,
        "torque": _display(values["torque"]) if telemetry.current else None,
        "temp": _display(values["temp"], digits=1) if telemetry.temperature else None,
    }


def _display(value: float | None, *, digits: int = 3) -> float | None:
    """配信の桁を画面で読める幅へ丸める (float の生桁は細い WiFi では純粋な無駄)。

    **丸めるのは配信の境界だけ。** 制御経路は元の float を見る。
    """
    return None if value is None else round(value, digits)


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


def _court_dependent_axes(sequence: Sequence) -> tuple[str, ...]:
    """位置定数を持たないシーケンス (テストの差し込み) は空で扱う。"""
    try:
        table = sequence.positions
    except RuntimeError:
        return ()
    return table.court_dependent_axes()


def _court_dependent_position_axes(sequence: Sequence) -> tuple[str, ...]:
    try:
        table = sequence.positions
    except RuntimeError:
        return ()
    return table.court_dependent_position_axes()


#: 位置定数 yaml を読み直して、変わった位置名 (`軸.位置名`) を返す。
#: 読めない・軸が変わったときは `PositionReloadError` を送出し、今の値を残す
PositionsReloader = Callable[[], tuple[str, ...]]


@dataclass
class RobotContext:
    sequence: Sequence
    can_manager: CANManager
    position_loops: list[M3508PositionLoop] = field(default_factory=list)
    sync_monitors: list[SyncMonitor] = field(default_factory=list)
    limit_monitors: list[LimitMonitor] = field(default_factory=list)
    target_refreshers: list[TargetRefresher] = field(default_factory=list)
    manual: ManualController | None = None
    mode: OperationMode = OperationMode.SEQUENCE
    #: 吸着に使うパッドの選択。持たないロボットは None (配信も null)
    suction: SuctionSelection | None = None
    #: 実機で位置定数を決めるための控え帳。位置定数を持たないロボットは None
    captures: PositionCaptureStore | None = None
    #: 指令の換算がコートで鏡になる軸。空ならこの台はコート未確定でも動かせる
    court_dependent_axes: tuple[str, ...] = ()
    #: コート別の値を持つ位置を抱える軸。位置名を引くときだけコートが要る
    court_dependent_position_axes: tuple[str, ...] = ()
    #: 位置定数 yaml を読み直す口。持たない台は None (UI にもボタンを出さない)
    reload_positions: PositionsReloader | None = None
    #: 最後に読み直した時刻と、そのとき変わった位置名
    positions_reloaded_at: float | None = None
    positions_reload_changed: tuple[str, ...] = ()


class RobotServer:
    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8080,
        *,
        health: HealthThresholds = DEFAULT_HEALTH,
        match_settings: MatchSettings = DEFAULT_MATCH,
        dry_run: bool = False,
        dev_tools: bool = False,
    ) -> None:
        self._host = host
        self._port = port
        self._app: web.Application | None = None
        self._robots: dict[str, RobotContext] = {}
        self._ws = WsHub()
        self._state_thinner = StateThinner()
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

        self.match = MatchState(settings=match_settings)

        self._health = health
        self._last_health: dict[str, HealthSnapshot] = {}

        self._motor_check = MotorCheckController(
            environment_deny=self._motor_check_environment_deny,
            pausables=self._motor_check_pausables,
            is_e_stop_active=lambda: self._e_stop_active,
            broadcast=self._ws.broadcast_json,
        )
        self._homing = HomingController(
            environment_deny=self._homing_environment_deny,
            broadcast=self._ws.broadcast_json,
        )
        self._switch_measure = SwitchMeasureController(
            environment_deny=self._switch_measure_environment_deny,
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
        limit_monitors: list[LimitMonitor] | None = None,
        target_refreshers: list[TargetRefresher] | None = None,
        manual: ManualController | None = None,
        suction: SuctionSelection | None = None,
        reload_positions: PositionsReloader | None = None,
    ) -> None:
        self._robots[name] = RobotContext(
            sequence=sequence,
            can_manager=can_manager,
            position_loops=list(position_loops or []),
            sync_monitors=list(sync_monitors or []),
            limit_monitors=list(limit_monitors or []),
            target_refreshers=list(target_refreshers or []),
            manual=manual,
            suction=suction,
            captures=self._make_capture_store(sequence, can_manager),
            court_dependent_axes=_court_dependent_axes(sequence),
            court_dependent_position_axes=_court_dependent_position_axes(sequence),
            reload_positions=reload_positions,
        )
        sequence.set_court(self.match.court)
        if manual is not None:
            manual.set_court(self.match.court)

    def _make_capture_store(
        self, sequence: Sequence, can_manager: CANManager
    ) -> PositionCaptureStore | None:
        try:
            table = sequence.positions
            motors = sequence.motors
        except RuntimeError:
            return None

        def stale_motors(names: Collection[str]) -> tuple[str, ...]:
            # 鮮度の判定は既存の単一情報源へ通す。時刻は 1 回だけ取る
            freshness = FeedbackFreshness(
                can_manager.last_feedback_at, timeout_ms=self._health.feedback_timeout_ms
            )
            now = freshness.now()
            return tuple(name for name in names if freshness.is_stale(name, now))

        return PositionCaptureStore(
            table,
            motors,
            court=lambda: self.match.court,
            stale_motors=stale_motors,
        )

    def set_motor_check_sequence(self, sequence: Sequence) -> None:
        self._motor_check.set_sequence(sequence, court=self.match.court)

    def set_homing_source(self, source: HomingSource) -> None:
        self._homing.set_source(source)
        self._switch_measure.set_source(source)

    def _apply_court(self) -> None:
        # 未確定 (None) もそのまま押し下げる。飛ばすと試合をリセットした後も
        # 前の試合のコートが各層に残り、解決済みのまま動いてしまう
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
        # 繋いだ直後の 1 通を全欄に戻す (差分は「相手が前回値を持っている」前提で落としている)
        self._state_thinner.request_full()
        logger.info("WebSocket 接続: %s", request.remote)

        for snapshot in (
            self._server_info_dict(),
            self.match.to_dict(),
            self._motor_check.payload(),
            self._homing.payload(),
            self._switch_measure.payload(),
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

        deny = spec.phase_deny_reason(self.match.phase)
        if deny is None and self._e_stop_active:
            deny = spec.e_stop_deny_reason()
        if deny is None:
            deny = self._manual_mode_deny_reason(spec, data)
        if deny is None:
            deny = self._reenergize_deny_reason(spec, data)
        if deny is None:
            deny = self._court_deny_reason(spec, data)
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

    def _court_deny_reason(self, spec: CommandSpec, data: dict) -> str | None:
        """コート未確定で拒否するか。**ゲートはロボット単位。**

        コートに依存しない台 (メインハンド) は退避路として動かせる必要があるので
        塞がない。`data["robot"]` が無いコマンドは両ハンドを走らせるため、
        1 台でも要るなら塞ぐ。要否の正は位置定数 (`PositionTable`) だけが持つ。
        """
        reason = spec.court_deny_reason()
        if reason is None or self.match.court is not None:
            return None

        robot_name = data.get("robot")
        if isinstance(robot_name, str):
            ctx = self._robots.get(robot_name)
            if ctx is None or not ctx.court_dependent_axes:
                return None
            return reason

        # 台を名指ししないコマンドは、コート別の位置を引く台でも途中で失敗する
        if any(
            ctx.court_dependent_axes or ctx.court_dependent_position_axes
            for ctx in self._robots.values()
        ):
            return reason
        return None

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

        busy = self._busy_label()
        if busy is not None:
            await self._reject_command(
                requester, "reenergize_motors", f"{busy}の実行中は再励磁できません"
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

    async def _cmd_homing_start(self, data: dict, requester: WSOrNone) -> None:
        reason = await self._homing.start(data.get("robot"), data.get("axes"))
        if reason is not None:
            await self._reject_command(requester, "homing_start", reason)

    async def _cmd_suction_pads_set(self, data: dict, requester: WSOrNone) -> None:
        robot_name = data.get("robot")
        if not isinstance(robot_name, str) or robot_name not in self._robots:
            return
        suction = self._robots[robot_name].suction
        if suction is None:
            await self._reject_command(
                requester, "suction_pads_set", f"'{robot_name}' に吸着パッドがありません"
            )
            return
        reason = suction.select(data.get("pads"))
        if reason is not None:
            await self._reject_command(requester, "suction_pads_set", reason)
            return
        logger.info(
            "吸着パッド選択: robot=%s 使用=%s", robot_name, ", ".join(suction.enabled()) or "なし"
        )

    async def _cmd_ping(self, data: dict, requester: WSOrNone) -> None:
        """往復時間の実測。送り主の目印をそのまま返すだけ (時計は合わせない)。"""
        if requester is None or requester.closed:
            return
        msg = json.dumps({"type": "pong", "t": data.get("t")}, ensure_ascii=False)
        if not await self._ws.send_or_drop(requester, msg):
            await self._ws.drop({requester})

    async def _cmd_position_capture(self, data: dict, requester: WSOrNone) -> None:
        robot_name = data.get("robot")
        if not isinstance(robot_name, str) or robot_name not in self._robots:
            return
        captures = self._robots[robot_name].captures
        if captures is None:
            await self._reject_command(
                requester, "position_capture", f"'{robot_name}' は位置定数を持っていません"
            )
            return
        reason = captures.capture(data.get("axis"), data.get("name"))
        if reason is not None:
            await self._reject_command(requester, "position_capture", reason)
            return
        logger.info(
            "位置を控えた: robot=%s 軸=%s 位置=%s", robot_name, data.get("axis"), data.get("name")
        )

    async def _cmd_positions_reload(self, data: dict, requester: WSOrNone) -> None:
        robot_name = data.get("robot")
        if not isinstance(robot_name, str) or robot_name not in self._robots:
            return
        ctx = self._robots[robot_name]
        if ctx.reload_positions is None:
            await self._reject_command(
                requester, "positions_reload", f"'{robot_name}' は位置定数を持っていません"
            )
            return

        # 読み直しの最中に軸を握っている点検やシーケンスがいると、次の 1 歩の行き先だけが
        # 差し替わる。止まっているあいだに入れ替える
        busy = self._busy_label()
        if busy is not None:
            await self._reject_command(
                requester, "positions_reload", f"{busy}の実行中は位置定数を読み直せません"
            )
            return
        if ctx.sequence.is_running:
            await self._reject_command(
                requester, "positions_reload", "シーケンスの実行中は位置定数を読み直せません"
            )
            return

        try:
            changed = ctx.reload_positions()
        except PositionReloadError as exc:
            logger.warning("位置定数の読み直しを拒否: robot=%s (%s)", robot_name, exc)
            await self._reject_command(requester, "positions_reload", str(exc))
            return

        ctx.positions_reloaded_at = time.time()
        ctx.positions_reload_changed = changed
        # コート別の値を持つ位置は yaml 側で増減しうる。ゲートの材料も入れ替える
        ctx.court_dependent_position_axes = _court_dependent_position_axes(ctx.sequence)
        logger.info(
            "位置定数を読み直した: robot=%s 変化 %d 件 (%s)",
            robot_name,
            len(changed),
            ", ".join(changed) or "なし",
        )
        await self._broadcast_state()

    async def _cmd_switch_measure_start(self, data: dict, requester: WSOrNone) -> None:
        reason = await self._switch_measure.start(data)
        if reason is not None:
            await self._reject_command(requester, "switch_measure_start", reason)

    async def _cmd_switch_distance_start(self, data: dict, requester: WSOrNone) -> None:
        reason = await self._switch_measure.start_distance(data)
        if reason is not None:
            await self._reject_command(requester, "switch_distance_start", reason)

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
            busy = self._busy_label()
            if busy is not None:
                await self._reject_command(
                    requester,
                    "set_operation_mode",
                    f"{busy}の実行中は手動操縦へ切り替えられません",
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
            ctx, axis, command, requester
        ):
            return None
        return ctx.manual, axis

    async def _allow_manual_in_sequence(
        self,
        ctx: RobotContext,
        axis: str,
        command: str,
        requester: WSOrNone,
    ) -> bool:
        manual = ctx.manual
        assert manual is not None
        try:
            always_manual = manual.is_always_manual(axis)
        except ManualControlError as exc:
            await self._reject_command(requester, command, str(exc))
            return False

        # トリガー待ちで止まっているあいだは wait_reached が走っていないので、位置指令の
        # 軸へ書いても到達判定を壊さない (箱の上で前後を詰める。2026-09-11 実機の求め)。
        # 次のステップは位置名で書き直すので、詰めた値が後の段へ持ち越されることもない
        if not always_manual and ctx.sequence.waiting_trigger:
            try:
                if manual.has_range(axis):
                    return True
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
        busy = self._busy_label()
        if busy is not None:
            await self._reject_command(requester, command, f"{busy}の実行中は手動で操作できません")
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
        except GuardViolation as exc:
            # 設計どおりの拒否。汎用の例外処理へ落とすと ERROR + Traceback で「失敗」に見える
            logger.warning("手動操縦を歯止めが拒否: %s (%s)", command, exc)
            await self._reject_command(requester, command, str(exc))
        except (CourtUnresolvedError, PositionLookupError) as exc:
            # コマンドゲートの内側にある 2 枚目。位置名を引く手動はゲートが
            # 塞がない台 (コート別の位置だけを持つ軸) でもここで止まる
            logger.warning("手動操縦をコート未確定が拒否: %s (%s)", command, exc)
            await self._reject_command(requester, command, str(exc))

    async def _cmd_set_court(self, data: dict, requester: WSOrNone) -> None:
        await self._handle_set_court(data, requester)

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
        return {
            "command": _display(handle.target),
            "command_mode": None if mode is None else mode.value,
        }

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
        return (
            *ctx.position_loops,
            *ctx.sync_monitors,
            *ctx.limit_monitors,
            *ctx.target_refreshers,
        )

    async def _handle_match_start(self, requester: WSOrNone = None) -> None:
        busy = self._busy_label()
        if busy is not None:
            await self._reject_command(
                requester, "match_start", f"{busy}の実行中は試合を開始できません"
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

        logger.info("試合開始: court=%s", court.value if (court := self.match.court) else "?")

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

        unenergized, unresponsive = self._split_inactive_motors(robot_name)

        return {
            "sync_violations": sorted(violations),
            "unenergized_motors": sorted(unenergized),
            "unresponsive_motors": sorted(unresponsive),
            "firmware_unconfirmed_motors": self._firmware_unconfirmed_motors(robot_name),
            "physical_stop": self._physical_stop_watch(robot_name),
            "failed_tasks": list(self._failed_tasks.get(robot_name, ())),
            "reenergizing": self._is_reenergizing(robot_name),
            "loops_running": all(loop.is_running for loop in ctx.position_loops),
            "monitors_running": all(monitor.is_running for monitor in ctx.sync_monitors),
            "limit_monitors_running": all(monitor.is_running for monitor in ctx.limit_monitors),
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
            "limit_monitors": [
                {
                    "axes": list(monitor.axis_names),
                    "running": monitor.is_running,
                    "stopped": sorted(monitor.stopped_axes),
                }
                for monitor in ctx.limit_monitors
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

    def _physical_stop_watch(self, robot_name: str) -> dict[str, object]:
        """物理非常停止 (DC 基板の `REF`) の押下を今検出できるか。

        検出経路は基板のフィードバックに載る緊急停止ビットだけなので、DC 基板が黙って
        いれば押されていても分からない。「分からない」を「押されていない」へ倒さず、
        かつ「途絶 = 押された」へも倒さない —— 後者は CAN の 1 通落ちで全体停止が掛かり、
        本番で機体が止まったまま戻せなくなる。倒す代わりに、検出手段が生きていないこと
        自体を配って画面に出す。

        物理停止を受けるピンは DC 用基板にしか無いので、監視元は `duty` の基板に限る。
        """
        ctx = self._robots[robot_name]
        sources = sorted(
            name
            for name, motor in ctx.can_manager.motors.items()
            if isinstance(motor, GenericDriver) and motor.control_type is ControlMode.DUTY
        )
        freshness = FeedbackFreshness(
            ctx.can_manager.last_feedback_at, timeout_ms=self._health.feedback_timeout_ms
        )
        now = freshness.now()
        unwatched = [name for name in sources if freshness.is_stale(name, now)]
        return {
            "watched": bool(sources) and not unwatched,
            "sources": sources,
            "unwatched": unwatched,
        }

    def _split_inactive_motors(self, robot_name: str) -> tuple[set[str], set[str]]:
        """励磁されていないモータを「無励磁」と「応答なし」へ仕分ける。

        手当てが逆なので 1 つの文面へ潰さない —— 応答が無い = ドライバの電源・CAN 配線 /
        励磁されていない = 再励磁。`lib/drivers/dm3520.py` の `activation_block_reason`
        が同じ理由で文面を 2 つに分けている。
        """
        since = self._energize_expected_since
        if self._e_stop_active or since is None:
            return set(), set()
        if time.time() - since < _ENERGIZE_GRACE_S:
            return set(), set()

        ctx = self._robots[robot_name]
        freshness = FeedbackFreshness(
            ctx.can_manager.last_feedback_at, timeout_ms=self._health.feedback_timeout_ms
        )
        now = freshness.now()

        candidates = {
            motor_name
            for motor_name, motor in ctx.can_manager.motors.items()
            if motor.is_energized() is False
        }
        candidates.update(self._inactive_motors.get(robot_name, ()))

        # 鮮度切れは消すのではなく「応答なし」へ移す。しかも `is_energized()` を一切見ない
        # —— 読む値はフレームを復号した瞬間にしか書かれず、途絶えてもクリアされないので、
        # 鮮度が切れた時点で True も False も信用できない。
        unresponsive = {name for name in candidates if freshness.is_stale(name, now)}

        fresh = candidates - unresponsive
        # 鮮度が生きているものだけ `is_energized()` を信用する。起動時に失敗した後で
        # 自力で励磁されたモータをラッチに居座らせない。
        energized = set()
        for motor_name in fresh:
            motor = ctx.can_manager.motors.get(motor_name)
            if motor is not None and motor.is_energized() is True:
                energized.add(motor_name)
        return fresh - energized, unresponsive

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
            # 停止中に取った「今の姿勢を保て」のラッチは信用できない値でありうる。
            # 励磁が届くまで黙らせ、励磁後の実測で取り直させる
            refreshed = [motor for r in ctx.target_refreshers for motor in r.motor_names]
            async with hold_target_refresh(
                ctx.target_refreshers, refreshed, reason="緊急停止解除の再励磁"
            ):
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

    def _environment_deny(self, command: str, what: str) -> str | None:
        """動かしてよい状況か。**動作確認と零点合わせで同じ判定を見る。**"""
        phase_deny = COMMANDS[command].phase_deny_reason(self.match.phase)
        if phase_deny is not None:
            return phase_deny

        if self._e_stop_active:
            return f"緊急停止中のため{what}を実行できません"

        for name in self._robots:
            if self._is_reenergizing(name):
                return f"'{name}' の再励磁が完了していないため{what}を実行できません"

        for name, ctx in self._robots.items():
            if ctx.mode is OperationMode.MANUAL:
                return f"'{name}' が手動操縦モードのため{what}を実行できません"
        for name, ctx in self._robots.items():
            if ctx.sequence.is_running:
                return f"'{name}' の通常シーケンス実行中のため{what}を実行できません"
        return None

    def _motor_check_environment_deny(self) -> str | None:
        return self._busy_deny("動作確認") or self._environment_deny(
            "motor_check_start", "動作確認"
        )

    def _homing_environment_deny(self) -> str | None:
        return self._busy_deny("零点合わせ") or self._environment_deny("homing_start", "零点合わせ")

    def _switch_measure_environment_deny(self) -> str | None:
        return self._busy_deny("作動点測定") or self._environment_deny(
            "switch_measure_start", "作動点測定"
        )

    def _axis_holders(self) -> tuple[tuple[str, bool], ...]:
        """軸を握りうる点検と、今それが走っているか。**相互排他はここ 1 箇所で決まる。**"""
        return (
            ("動作確認", self._motor_check.running),
            ("零点合わせ", self._homing.running),
            ("作動点測定", self._switch_measure.running),
        )

    def _busy_deny(self, what: str) -> str | None:
        for label, running in self._axis_holders():
            if running and label != what:
                return f"{label}の実行中は{what}を実行できません"
        return None

    def _busy_label(self) -> str | None:
        """今この瞬間、軸を握っている点検があればその名前。"""
        for label, running in self._axis_holders():
            if running:
                return label
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
            started = time.monotonic()
            try:
                await self._broadcast_state()
                await self._motor_check.publish()
                await self._homing.publish()
                await self._switch_measure.publish()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("状態配信に失敗しました (配信は継続します)")
            await asyncio.sleep(self._broadcast_delay(time.monotonic() - started))

    def _broadcast_delay(self, elapsed: float) -> float:
        """送信に食われた時間の分だけ次の周期を空ける。

        細い WiFi では送信そのものが遅れる。そこへ 20Hz で積み増すと送信上限
        (`_WS_SEND_TIMEOUT_S`) に当たって操縦者が切り離される —— 表示の滑らかさより
        繋がり続ける方を採る。速さが戻れば次の周期で元に戻る (状態を持たない)。
        """
        return max(self._broadcast_interval, min(elapsed * 2.0, _BROADCAST_MAX_INTERVAL_S))

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
                    # 読めていないフィードバックからは押下を主張しない。取りこぼしうる
                    # ことは `_physical_stop_watch` が safety へ載せて画面に出す。
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
            state_messages.append(
                self._state_thinner.thin(self._build_state_message(robot_name, snapshot=snap))
            )

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
            "suction": ctx.suction.to_dict(court=self.match.court)
            if ctx.suction is not None
            else None,
            # 実機で決める位置定数の控え帳。控えられる軸と位置名もサーバーが配る
            # (UI が位置名を書き写すと、yaml へ足した名前が片方の画面に出ない)
            "position_capture": ctx.captures.to_dict() if ctx.captures is not None else None,
            # 位置定数 yaml の読み直し。口を持たない台は null (UI はボタンを出さない)
            "positions_reload": self._positions_reload_state(robot_name),
            # この台がコート確定を要るか。UI が軸名から導き直さないようサーバーが配る
            "court_required": bool(ctx.court_dependent_axes),
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

    def _positions_reload_state(self, robot_name: str) -> dict | None:
        ctx = self._robots[robot_name]
        if ctx.reload_positions is None:
            return None
        return {
            "reloaded_at": ctx.positions_reloaded_at,
            "changed": list(ctx.positions_reload_changed),
        }

    def _manual_state(self, robot_name: str) -> dict:
        ctx = self._robots[robot_name]
        axes = ctx.manual.axes_info() if ctx.manual is not None else []
        for axis in axes:
            # 画面の桁へ丸める。生桁のままだと、表示に出ない揺れで軸行が毎フレーム再送される
            for key in ("value", "target", "deviation"):
                axis[key] = _display(axis.get(key))
        return {"mode": ctx.mode.value, "axes": axes}

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
