from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import pathlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from aiohttp import WSMsgType, web

from lib.can_manager import CANManager
from lib.commands import CommandSpec, RejectChannel, phase_deny_reason, spec_for
from lib.config_schema import (
    DEFAULT_HEALTH,
    DEFAULT_MATCH,
    DEFAULT_MOTOR_CHECK,
    HealthThresholds,
    MatchSettings,
)
from lib.control.position_loop import (
    MAX_TUNABLE_GAIN,
    TUNABLE_PID_KEYS,
    M3508PositionLoop,
    PidGains,
)
from lib.control.sync_monitor import SyncMonitor
from lib.control.target_refresh import GenericTargetRefresher
from lib.drivers.generic import GenericDriver
from lib.health import (
    BusHealth,
    BusHealthInfo,
    CheckRunSnapshot,
    HealthSnapshot,
    MotorCheckRecord,
    MotorHealth,
    MotorHealthInfo,
)
from lib.manual import ManualControlError, ManualController, OperationMode
from lib.match_state import ChecklistItem, Court, MatchState
from lib.motor_check import MotorCheckRunner
from lib.sequence.engine import Sequence

logger = logging.getLogger(__name__)

_WEB_DIST_DIR = pathlib.Path(__file__).resolve().parent.parent / "web" / "dist"

#: 1 クライアントへの送信を諦めるまでの秒数。
#: テレメトリは 20Hz なので、1 秒返ってこない相手は既に落ちているとみなしてよい。
_WS_SEND_TIMEOUT_S = 1.0

#: 拒否通知の宛先。HTTP POST や内部の安全機構からの呼び出しには返す相手が居ない。
type WSOrNone = web.WebSocketResponse | None


# overall を最悪値で集約するためのランク。lib.health._BUS_SEVERITY_RANK と一致させる
# (重複定義を避けたいが、health.py 側を private 扱いにしているため局所コピーする)。
_BUS_SEVERITY_RANK: dict[BusHealth, int] = {
    BusHealth.OK: 0,
    BusHealth.DEGRADED: 1,
    BusHealth.DOWN: 2,
}


def _level_for_state(state: BusHealth) -> str:
    """BusHealth を health_change イベントの level 文字列にマップする。"""
    if state is BusHealth.DOWN:
        return "critical"
    if state is BusHealth.DEGRADED:
        return "warning"
    return "info"


def _level_for_motor_state(state: MotorHealth) -> str:
    """MotorHealth を health_change イベントの level 文字列にマップする。"""
    if state is MotorHealth.FAULT:
        return "critical"
    if state in (MotorHealth.STALE, MotorHealth.WARNING):
        return "warning"
    return "info"


@dataclass
class RobotContext:
    sequence: Sequence
    can_manager: CANManager
    # そのロボットの M3508 位置制御ループ (バスごと 1 本)。動作確認中は
    # 0x200 フレームの奪い合いになるため一時停止させる
    position_loops: list[M3508PositionLoop] = field(default_factory=list)
    # そのロボットの同期監視。ラッチの解除経路がサーバー側に無いと、一度ずれを
    # 検知した軸は二度と発報せず、操縦者は無監視のまま機体を動かすことになる
    sync_monitors: list[SyncMonitor] = field(default_factory=list)
    # 自作モタドラ向けの目標値再送。動作確認中は確認用の指令を打ち消すため止め、
    # 緊急停止時は保持した目標を捨てる (解除だけで動き出させない)
    target_refreshers: list[GenericTargetRefresher] = field(default_factory=list)
    # 手動操縦の指令口。位置定数を読めていないロボットでは None (手動不可)
    manual: ManualController | None = None
    # 制御権を誰が握っているか。ロボットごとに独立させる (片方だけ手動が成立する)。
    # **正はサーバー側に置く。** 操縦者 2 名 + Monitor が別ブラウザで繋がるため、
    # クライアント側に持つと「片方の画面だけが手動」という、Monitor から機体の
    # 動きが説明できない状態が作れてしまう
    mode: OperationMode = OperationMode.SEQUENCE


class RobotServer:
    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8080,
        *,
        health: HealthThresholds = DEFAULT_HEALTH,
        motor_check_per_motor_timeout_ms: float = DEFAULT_MOTOR_CHECK.per_motor_timeout_ms,
        motor_check_default_magnitude: dict[str, float] | None = None,
        motor_check_per_motor_overrides: dict[str, dict[str, float]] | None = None,
        checklist_definitions: dict[str, list[ChecklistItem]] | None = None,
        match_settings: MatchSettings = DEFAULT_MATCH,
        dry_run: bool = False,
        dev_tools: bool = False,
    ) -> None:
        self._host = host
        self._port = port
        self._app: web.Application | None = None
        self._robots: dict[str, RobotContext] = {}
        self._ws_clients: set[web.WebSocketResponse] = set()
        #: 切り離し中のクライアントを閉じるタスク。GC で消えないよう参照を保持する
        self._closing_tasks: set[asyncio.Task[None]] = set()
        self._broadcast_interval: float = 0.05
        self._broadcast_task: asyncio.Task[None] | None = None
        self._e_stop_active: bool = False
        # 停止理由は停止が続くかぎり保持する。`_broadcast_state` は停止中に毎ティック
        # e_stop_state を送り直すため、保持しないと自動検知の直後 1 通だけが本当の
        # 原因を載せ、以降の再配信が UI の表示を「操縦者の停止操作」へ塗り替える
        self._e_stop_reason: str | None = None
        # 基板が報告する緊急停止 (FEEDBACK の緊急停止ビット) を、この時刻より後に届いた
        # フィードバックについてのみ信用する。解除操作は「解除フレーム送信 →
        # 基板がラッチを外す → 次の FEEDBACK」の順に伝わるので、送信より前の
        # フィードバックに残った緊急停止ビットをそのまま信じると、解除した瞬間に
        # サーバーが自分で緊急停止をかけ直して二度と解除できなくなる
        self._board_e_stop_ignore_before: float = 0.0
        # dry-run 時はモータ状態とヘルスを擬似的に揺らがせて Web UI の描画を成立させる。
        # 実機運用時は False のまま影響しない。
        self._dry_run: bool = dry_run
        # 開発用コマンド (指差喚呼の一括チェック等) の解禁。試合運用の手順を飛ばすので
        # 既定は False で、起動時に明示したときだけ立つ。UI へは server_info で配る
        self._dev_tools: bool = dev_tools
        self._sequence_tasks: dict[str, asyncio.Task[None]] = {}

        # 試合全体の状態 (コート / フェーズ / チェックリスト)。
        # 操縦者 2 名 + Monitor が別ブラウザで接続するため正はサーバー側に置く。
        self.match = MatchState(definitions=checklist_definitions, settings=match_settings)

        # ヘルスチェックしきい値は config/system.yaml の health セクション由来。
        # 4 値を分解せず 1 つの値のまま持つ (config_schema.HealthThresholds 参照)
        self._health = health
        # 直近の HealthSnapshot をロボット名で保持し、_diff_health で前回と比較する
        self._last_health: dict[str, HealthSnapshot] = {}

        # アクチュエータ動作確認 (Phase 6 段階⑨/⑩)。
        # 設定値は段階⑩ で config/*.yaml から流し込まれる前提でキーワード引数化しておく。
        self._motor_check_settings: dict[str, object] = {
            "per_motor_timeout_ms": motor_check_per_motor_timeout_ms,
            "default_magnitude": motor_check_default_magnitude,
            "per_motor_overrides": motor_check_per_motor_overrides or {},
        }
        # ロボットごとの実行中ランナー (排他制御用)
        self._motor_check_runners: dict[str, MotorCheckRunner] = {}
        # 直近結果の保持。GET /robots/{robot}/motor_check/last はここを参照する
        self._motor_check_last: dict[str, CheckRunSnapshot] = {}
        # asyncio.create_task で起動した実行タスク。シャットダウン時にキャンセルする
        self._motor_check_tasks: dict[str, asyncio.Task[None]] = {}

    @property
    def dev_tools(self) -> bool:
        """開発用コマンドが解禁されているか。コマンドゲートと server_info が参照する。"""
        return self._dev_tools

    @property
    def e_stop_active(self) -> bool:
        """緊急停止状態。モータ指令経路のインターロックがこの値を参照する。

        書き換えは e_stop / e_stop_release コマンド経由に限りたいため読み取り専用。
        """
        return self._e_stop_active

    def add_robot(
        self,
        name: str,
        sequence: Sequence,
        can_manager: CANManager,
        position_loops: list[M3508PositionLoop] | None = None,
        sync_monitors: list[SyncMonitor] | None = None,
        target_refreshers: list[GenericTargetRefresher] | None = None,
        manual: ManualController | None = None,
    ) -> None:
        self._robots[name] = RobotContext(
            sequence=sequence,
            can_manager=can_manager,
            position_loops=list(position_loops or []),
            sync_monitors=list(sync_monitors or []),
            target_refreshers=list(target_refreshers or []),
            manual=manual,
        )
        sequence.set_court(self.match.court)
        if manual is not None:
            manual.set_court(self.match.court)

    def _apply_court(self) -> None:
        for ctx in self._robots.values():
            ctx.sequence.set_court(self.match.court)
            # 手動のプリセットもコート別定義を持つ。流し忘れると、コートを変えた後の
            # 手動操作だけが反対コートの座標へ機体を運ぶ
            if ctx.manual is not None:
                ctx.manual.set_court(self.match.court)

    def create_app(self) -> web.Application:
        app = web.Application()
        # ヘルスエンドポイントは静的ファイル SPA フォールバック (`/{path:.*}`) より先に
        # 登録する必要がある。先に SPA ルートを登録すると `/health` が index.html に
        # 吸い込まれて 200 HTML になり、監視ツールが誤判定する。
        app.router.add_get("/health", self._health_handler)
        app.router.add_get("/ws", self._ws_handler)
        # 動作確認エンドポイントも SPA フォールバックより前に登録する
        app.router.add_post("/robots/{robot}/motor_check", self._motor_check_post)
        app.router.add_get("/robots/{robot}/motor_check/last", self._motor_check_get_last)

        if _WEB_DIST_DIR.is_dir():
            app.router.add_static("/assets", _WEB_DIST_DIR / "assets")
            app.router.add_get("/{path:.*}", self._spa_handler)

        app.on_startup.append(self._on_startup)
        app.on_shutdown.append(self._on_shutdown)

        self._app = app
        return app

    async def _spa_handler(self, request: web.Request) -> web.StreamResponse:
        """SPA フォールバック: 静的ファイルがあればそれを返し、なければ index.html を返す"""
        path = request.match_info.get("path", "")
        file_path = _WEB_DIST_DIR / path
        if path and file_path.is_file():
            return web.FileResponse(file_path)
        return web.FileResponse(_WEB_DIST_DIR / "index.html")

    async def _health_handler(self, request: web.Request) -> web.Response:
        """GET /health: 全ロボットの HealthSnapshot を集約し、最悪値で 200/503 を決める。

        CI・監視ツール・curl 動作確認用。WS が使えない環境向けの代替経路。
        """
        robots_payload: dict[str, dict] = {}
        worst_rank = 0
        for robot_name in self._robots:
            snap = self._compute_health(robot_name)
            robots_payload[robot_name] = snap.to_dict()
            worst_rank = max(worst_rank, _BUS_SEVERITY_RANK[snap.overall])

        overall = BusHealth.OK
        for state, rank in _BUS_SEVERITY_RANK.items():
            if rank == worst_rank:
                overall = state
                break

        # OK 以外は監視系から異常を検出できるよう 503 を返す
        status = 200 if overall is BusHealth.OK else 503
        return web.json_response(
            {"overall": overall.value, "robots": robots_payload},
            status=status,
        )

    async def _on_startup(self, app: web.Application) -> None:
        self._broadcast_task = asyncio.create_task(self._broadcast_loop())
        # 各ロボットのシーケンス常駐ループを起動。停止/ジャンプで再起動可能な
        # 永続タスクとして保持し、shutdown でキャンセルする。
        # ループ本体 (開始要求待ち・停止後の巻き戻し) はシーケンス側の責務。
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

        for task in self._closing_tasks:
            task.cancel()
        self._closing_tasks.clear()

        await self._close_all_clients()

    async def _close_all_clients(self) -> None:
        """接続中のクライアントを全て閉じる (終了処理の共通経路)。

        `close()` は相手のクローズ応答を待つため、スリープしたノート PC が 1 台
        繋がっているだけで終了処理が返らなくなり、CAN を落とす後始末まで到達しない。
        送信と同じ上限を通し、複数台をまとめて待つことで最悪でも上限 1 回ぶんで抜ける。
        """
        clients = set(self._ws_clients)
        self._ws_clients.clear()
        if clients:
            await asyncio.gather(*(self._close_quietly(ws) for ws in clients))

    async def _ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._ws_clients.add(ws)
        logger.info("WebSocket 接続: %s", request.remote)

        # server_info と match_state は接続直後にしか送らない。
        # server_info は起動オプション由来で試合中に変わらないため定期配信に載せず、
        # match_state は変化時のみ配信するのでスナップショットが要る
        # (これがないとリロード直後のクライアントが現在のモード/フェーズを知れない)。
        try:
            await ws.send_str(json.dumps(self._server_info_dict(), ensure_ascii=False))
            await ws.send_str(json.dumps(self.match.to_dict(), ensure_ascii=False))
        except ConnectionResetError:
            self._ws_clients.discard(ws)
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
            self._ws_clients.discard(ws)
            logger.info("WebSocket 切断: %s", request.remote)

        return ws

    def _server_info_dict(self) -> dict:
        """起動オプション由来の、試合中に変わらない情報。接続直後に 1 度だけ送る。

        開発用ボタンの表示可否をクライアント側のビルド時定数で決めると、同じ
        `web/dist` を配る本番と開発で再ビルドが要る (= 切り替えとして機能しない)。
        正はサーバーが持ち、UI は配られた値を表示に反映するだけにする。
        """
        return {
            "type": "server_info",
            "dev_tools": self._dev_tools,
            "dry_run": self._dry_run,
        }

    async def handle_command(
        self,
        data: dict,
        *,
        requester: web.WebSocketResponse | None = None,
    ) -> None:
        """1 コマンドを受理して 2 段のゲートに掛け、通ったものだけ実行する。

        操縦者のコマンドがサーバーへ入る唯一の口。経路 (WS / HTTP / 内部の
        安全機構) に依らずここへ合流させることで、ゲートを通らない実行経路が
        生まれない。``activate_e_stop`` を公開しているのと同じ理由で公開する:
        受理判定は入口ごとに書き直してよいものではない。

        Args:
            requester: 要求元のクライアント。拒否通知の宛先に使う。HTTP POST や
                内部からの呼び出しには返す相手がいないため None を許す。
        """
        spec = spec_for(data.get("type"))
        if spec is None:
            # 語彙に無いコマンドはハンドラへ到達させない (拒否理由も返さない)
            logger.debug("未知のコマンド: %s", data.get("type"))
            return

        # ゲートは 3 段。開発用ゲート (この起動にそのコマンドが存在するか) が最初で、
        # 次にフェーズゲート (試合進行として許されるか)、通ったものだけ緊急停止ゲート
        # (今モータを動かしてよいか) に掛ける。フェーズが MATCH のままでも緊急停止中は
        # START を通してはならず、match_start は READY で受理されうるのでフェーズ遷移より
        # 手前で止める。開発用ゲートを先頭に置くのは、無効な起動での拒否理由が
        # 「フェーズが違う」ではなく「この起動には無い機能」であるべきだから。
        deny = spec.dev_tools_deny_reason(self._dev_tools)
        if deny is None:
            deny = spec.phase_deny_reason(self.match.phase)
        if deny is None and self._e_stop_active:
            deny = spec.e_stop_deny_reason()
        if deny is not None:
            logger.info("コマンド拒否: %s (%s)", spec.name, deny)
            await self._reject_by_channel(spec, data, requester, deny)
            return

        handler: Callable[[dict, web.WebSocketResponse | None], Awaitable[None]] = getattr(
            self, spec.handler
        )
        await handler(data, requester)

    async def _reject_by_channel(
        self,
        spec: CommandSpec,
        data: dict,
        requester: web.WebSocketResponse | None,
        reason: str,
    ) -> None:
        """拒否を、そのコマンドが宣言した経路で操縦者へ返す。"""
        if spec.reject_channel is RejectChannel.MOTOR_CHECK_ERROR:
            await self._broadcast_motor_check_error(str(data.get("robot")), reason)
        else:
            await self._reject_command(requester, spec.name, reason)

    # ------------------------------------------------------------------ #
    #  コマンドハンドラ (lib/commands.py の CommandSpec.handler から引かれる)
    #  ゲートは handle_command で済んでいるので、ここでは実行だけを行う。
    # ------------------------------------------------------------------ #

    async def _cmd_trigger(self, data: dict, _requester: WSOrNone = None) -> None:
        robot_name = data.get("robot")
        if robot_name and robot_name in self._robots:
            self._robots[robot_name].sequence.trigger()
            logger.info("trigger: %s", robot_name)

    async def _cmd_e_stop(self, _data: dict, _requester: WSOrNone = None) -> None:
        await self.activate_e_stop()

    async def _cmd_e_stop_release(self, _data: dict, requester: WSOrNone = None) -> None:
        if not self._e_stop_active:
            # 「解除」は解除すべき状態があるときだけ通す。停止していない試合中に
            # 1 通届くだけで同期ずれラッチが全解除され、全モータへ再励磁が飛ぶ
            # (リロード直後の UI やリトライで実際に起こりうる)
            await self._reject_command(requester, "e_stop_release", "緊急停止中ではありません")
            return

        logger.info("緊急停止解除コマンド受信")
        # フラグを落とす前にラッチを外す。逆順だと「復帰した」と配信した後にも
        # ラッチが残る周期が生まれ、その間 y_axis だけが動かない機体になる。
        # 停止中は電流 0 が維持されるので、先に外しても機体は動き出さない
        self._reset_sync_latches()
        self._e_stop_active = False
        self._e_stop_reason = None
        await self._broadcast_e_stop_state()
        await self._reactivate_motors()
        # 解除フレームはこの時点で送り終えている。以降に届いたフィードバックで
        # まだ緊急停止ビットが立っていれば、それは基板側にまだ止まる理由がある
        # (物理停止スイッチが押されたまま等) ということなので、改めて停止させる
        self._board_e_stop_ignore_before = time.time()

    async def _cmd_health_check(self, _data: dict, _requester: WSOrNone = None) -> None:
        # クライアントからの即時ヘルス要求。次回ループを待たずに即配信する。
        await self._broadcast_state()

    async def _cmd_set_param(self, data: dict, requester: WSOrNone = None) -> None:
        await self._handle_set_param(data, requester)

    async def _cmd_sequence_jump(self, data: dict, _requester: WSOrNone = None) -> None:
        robot_name = data.get("robot")
        step_index = data.get("step_index")
        # bool は Python では int だが、ステップ番号として送られてきた時点で誤送信。
        # True を通すと index 1 として受理され、停止中のシーケンスが叩き起こされて
        # 誰も開始していないのに 2 番目のステップから機体が動き出す
        if isinstance(step_index, bool) or not isinstance(step_index, int):
            return
        if robot_name and robot_name in self._robots:
            self._robots[robot_name].sequence.request_jump(step_index)
            logger.info("sequence_jump: %s -> %d", robot_name, step_index)

    async def _cmd_sequence_stop(self, data: dict, _requester: WSOrNone = None) -> None:
        robot_name = data.get("robot")
        if robot_name and robot_name in self._robots:
            self._robots[robot_name].sequence.request_stop()
            logger.info("sequence_stop: %s", robot_name)

    async def _cmd_sequence_start(self, data: dict, _requester: WSOrNone = None) -> None:
        robot_name = data.get("robot")
        if robot_name and robot_name in self._robots:
            self._robots[robot_name].sequence.request_start()
            logger.info("sequence_start: %s", robot_name)

    async def _cmd_motor_check_start(self, data: dict, _requester: WSOrNone = None) -> None:
        robot_name = data.get("robot")
        # 知らないロボットは silent ignore (ws を切断しないため)
        if robot_name and robot_name in self._robots:
            await self._start_motor_check(robot_name)

    async def _cmd_motor_check_abort(self, data: dict, _requester: WSOrNone = None) -> None:
        robot_name = data.get("robot")
        if robot_name and robot_name in self._motor_check_runners:
            self._motor_check_runners[robot_name].abort()

    # ------------------------------------------------------------------ #
    #  手動操縦
    # ------------------------------------------------------------------ #

    async def _cmd_set_operation_mode(self, data: dict, requester: WSOrNone = None) -> None:
        robot_name = data.get("robot")
        if not isinstance(robot_name, str) or robot_name not in self._robots:
            # 知らないロボットは silent ignore (WS を切断しないため)
            return
        try:
            mode = OperationMode(data.get("mode"))
        except ValueError:
            await self._reject_command(
                requester, "set_operation_mode", f"未知の操作モード: {data.get('mode')!r}"
            )
            return
        await self._apply_operation_mode(robot_name, mode, requester=requester)

    async def _cmd_manual_move(self, data: dict, requester: WSOrNone = None) -> None:
        target = await self._manual_target(data, "manual_move", requester)
        if target is None:
            return
        manual, axis = target
        position = data.get("position")
        if not isinstance(position, str) or not position:
            await self._reject_command(requester, "manual_move", "位置名が指定されていません")
            return
        await self._run_manual("manual_move", requester, manual.move_to_position(axis, position))

    async def _cmd_manual_set(self, data: dict, requester: WSOrNone = None) -> None:
        target = await self._manual_target(data, "manual_set", requester)
        if target is None:
            return
        manual, axis = target
        value = await self._manual_number(data, "value", "manual_set", requester)
        if value is None:
            return
        await self._run_manual("manual_set", requester, manual.set_value(axis, value))

    async def _cmd_manual_jog(self, data: dict, requester: WSOrNone = None) -> None:
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
        """1 ロボットの制御権を切り替える。切り替えたら True。

        手動へ入る条件は「今このロボットの制御権を他の誰も握っていないこと」に尽きる。
        シーケンスは止めれば手放せるが、動作確認は 1 台ずつ駆動する途中で奪えないので
        拒否する (止めたければ motor_check_abort が別にある)。

        **モータの目標値は切り替えでは消さない。** 消すと保持トルクを失い、
        昇降軸が自重で落ちる。切り替えで消すのはジョグの起点だけ。
        """
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
            # 二重起動の判定と同じく実行タスクの生死で見る。runner の is_running は
            # 登録から run() 開始までのあいだ False で、そこを素通しすると
            # 駆動中の動作確認と手動指令が同じモータを奪い合う
            task = self._motor_check_tasks.get(robot_name)
            if task is not None and not task.done():
                await self._reject_command(
                    requester,
                    "set_operation_mode",
                    "動作確認の実行中は手動操縦へ切り替えられません",
                )
                return False
            # 制御権を必ず手放させる。破棄しないと、切替直前に届いた開始要求が
            # 手動で機構を動かしている最中に発火する
            self._stop_sequence(ctx, discard_pending_start=True)

        ctx.mode = mode
        if ctx.manual is not None:
            # 起点を捨てる。手動へ入る側では「シーケンスが動かした後の現在値」から
            # 取り直させ、抜ける側では古い起点を次回まで持ち越させない
            ctx.manual.reset()
        logger.info("操作モード変更: robot=%s mode=%s", robot_name, mode.value)
        return True

    async def _manual_target(
        self,
        data: dict,
        command: str,
        requester: WSOrNone,
    ) -> tuple[ManualController, str] | None:
        """手動指令の宛先を解決する。受理できなければ理由を返して None。

        モード判定をここ 1 箇所に置く。ハンドラごとに書くと、足したコマンドだけが
        半自動運転中でも通る経路になる。
        """
        robot_name = data.get("robot")
        if not isinstance(robot_name, str) or robot_name not in self._robots:
            return None

        ctx = self._robots[robot_name]
        if ctx.manual is None:
            await self._reject_command(
                requester, command, f"'{robot_name}' は手動操縦に対応していません"
            )
            return None
        if ctx.mode is not OperationMode.MANUAL:
            await self._reject_command(
                requester, command, "手動操縦モードではありません (モードを切り替えてください)"
            )
            return None

        axis = data.get("axis")
        if not isinstance(axis, str) or not axis:
            await self._reject_command(requester, command, "軸が指定されていません")
            return None
        return ctx.manual, axis

    async def _manual_number(
        self,
        data: dict,
        key: str,
        command: str,
        requester: WSOrNone,
    ) -> float | None:
        """手動指令の数値を取り出す。受理できなければ理由を返して None。

        NaN / inf を弾くのは、比較がすべて false になってクランプを素通りするため。
        一度内部へ入ると「無言で止まったモータ」になり、診断ビットにも現れない
        (CAN 上を float が 1 バイトも流れない設計と同じ理由)。
        """
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
        """手動指令を実行し、拒否理由を要求元へ返す。

        例外をここで受け止めるのは、``handle_command`` が ``_ws_handler`` の
        受信ループから await されているため。抜けさせると操縦者の WS が切れ、
        軸名を打ち間違えただけで画面ごと落ちる。
        """
        try:
            await coro
        except ManualControlError as exc:
            await self._reject_command(requester, command, str(exc))
        except Exception as exc:
            # 位置名の誤り (PositionLookupError)・緊急停止の競合・送信失敗など。
            # 握り潰さずログには必ず残す (原因が操縦者の画面からは追えない)
            logger.exception("手動指令に失敗: %s", command)
            # 例外クラス名を操縦者へ出しても復旧の判断材料にならない。空なら定型文にする
            await self._reject_command(requester, command, str(exc) or "手動指令に失敗しました")

    async def _cmd_set_court(self, data: dict, requester: WSOrNone = None) -> None:
        await self._handle_set_court(data, requester)

    async def _cmd_checklist_set(self, data: dict, _requester: WSOrNone = None) -> None:
        role = data.get("role")
        item_id = data.get("item_id")
        checked = bool(data.get("checked"))
        if isinstance(role, str) and isinstance(item_id, str):
            if self.match.set_checklist_item(role, item_id, checked):
                await self._broadcast_match_state()
            else:
                logger.warning("未知のチェック項目: role=%s item=%s", role, item_id)

    async def _cmd_checklist_check_all(self, data: dict, _requester: WSOrNone = None) -> None:
        role = data.get("role")
        self.match.check_all_checklist_items(role if isinstance(role, str) else None)
        # 指差喚呼を飛ばしたことは必ずログに残す。試合直前のログを追ったときに
        # 「点検を実施したのか、開発用ボタンで埋めたのか」が区別できないと困る
        logger.warning("開発用: 指差喚呼を一括チェックしました (role=%s)", role or "all")
        await self._broadcast_match_state()

    async def _cmd_checklist_reset(self, data: dict, _requester: WSOrNone = None) -> None:
        role = data.get("role")
        self.match.reset_checklist(role if isinstance(role, str) else None)
        await self._broadcast_match_state()

    async def _cmd_match_start(self, _data: dict, requester: WSOrNone = None) -> None:
        await self._handle_match_start(requester)

    async def _cmd_match_finish(self, _data: dict, _requester: WSOrNone = None) -> None:
        if self.match.match_finish():
            logger.info("試合終了")
            self._stop_all_sequences()
            await self._broadcast_match_state()

    async def _cmd_match_reset(self, _data: dict, requester: WSOrNone = None) -> None:
        self.match.match_reset()
        logger.info("セッティングタイムへ復帰")
        self._stop_all_sequences()
        # 復帰操作は既知の状態へ戻すもの。手動のまま次の試合の準備に入ると、
        # 操縦者が切り替えたことを忘れたまま sequence_start が無反応になる
        for robot_name in self._robots:
            await self._apply_operation_mode(
                robot_name, OperationMode.SEQUENCE, requester=requester
            )
        self._apply_court()
        await self._broadcast_match_state()

    async def _handle_set_param(
        self,
        data: dict,
        requester: web.WebSocketResponse | None,
    ) -> None:
        """PID ゲインを実行中に差し替える (/pid-tuning タブ)。

        3 値を 1 通で受ける。項目ごとに分けると混ざった状態が 200Hz の制御周期を
        またいで残り、通らないときの拒否も 3 通に増える。

        対象は M3508 だけ。位置制御を PC 側の PIDController で閉じているのは M3508 の
        位置制御ループのみで、EDULITE 05 と自作モータドライバはドライバ/ファーム側で
        ループを閉じているため PC 側に書き換えられるゲインが存在しない
        (自作モタドラの SET_PARAM は PC 側にエンコーダが無く同じ意味を持たない)。

        通らない要求は必ず理由を返す。以前のように黙ってログを出すだけだと、操縦者は
        送信できたと信じたまま効いていないゲインで調整を続けることになる。
        """
        motor_name = data.get("motor")
        gains = data.get("gains")

        if not isinstance(motor_name, str) or not motor_name:
            await self._reject_command(requester, "set_param", "モータが指定されていません")
            return

        if not isinstance(gains, dict) or not gains:
            await self._reject_command(
                requester, "set_param", "差し替えるゲインが指定されていません"
            )
            return

        reason = self._invalid_gain_reason(gains)
        if reason is not None:
            await self._reject_command(requester, "set_param", reason)
            return

        loop = self._find_position_loop(motor_name)
        if loop is None:
            if self._has_motor(motor_name):
                reason = (
                    f"モータ '{motor_name}' は PC 側 PID を持ちません "
                    "(ドライバ側で制御しているためサーバーからは変更できません)"
                )
            else:
                reason = f"モータ '{motor_name}' が見つかりません"
            await self._reject_command(requester, "set_param", reason)
            return

        affected = loop.set_pid_gains(motor_name, gains)
        applied = ", ".join(f"{key}={gains[key]}" for key in sorted(gains))
        logger.info("set_param: %s を適用 (%s)", applied, ", ".join(affected))

    def _invalid_gain_reason(self, gains: dict) -> str | None:
        """受け取ったゲイン一式を検証する。問題が無ければ None。

        1 つでも通らなければ 1 つも適用しない。半分だけ入ると、操縦者が意図しない
        PID の組み合わせで機体が動く。
        """
        for key, value in gains.items():
            if not isinstance(key, str) or key not in TUNABLE_PID_KEYS:
                return f"変更できるのは {'/'.join(TUNABLE_PID_KEYS)} のみです (受け取った: {key!r})"

            # bool は Python では int だが、ゲインとして送られてきた時点で誤送信
            if isinstance(value, bool) or not isinstance(value, int | float):
                return f"{key} の値が数値ではありません: {value!r}"
            if not math.isfinite(value):
                return f"{key} の値が有限ではありません: {value!r}"
            if value < 0:
                # 負のゲインは正帰還になり、偏差が増える向きに電流が出て即座に発散する
                return f"{key} に負の値は指定できません: {value}"
            if value > MAX_TUNABLE_GAIN:
                # 上限が無いと kp=1e6 のような打ち間違いがそのまま通る。出力は
                # ±CURRENT_MAX に飽和するので、その先は調整ではなくバンバン制御になる
                return f"{key} の上限は {MAX_TUNABLE_GAIN:.0f} です (受け取った: {value})"
        return None

    def _motor_pid_state(self, motor_name: str) -> PidGains | None:
        """UI へ配る現在ゲイン。PC 側 PID を持たないモータは None。

        配らなかった頃、/pid-tuning は現在値を知る手段が無いまま初期値 0 を表示し、
        そのまま送ると全ゲインが 0 になった。None は「ドライバ・ファーム側で
        制御していて PC からは変更できない」の単一の表現でもあり、UI はこれだけで
        調整対象を選り分ける (判定を UI 側に書き写さない)。
        """
        loop = self._find_position_loop(motor_name)
        return None if loop is None else loop.pid_gains(motor_name)

    def _find_position_loop(self, motor_name: str) -> M3508PositionLoop | None:
        """指定モータを制御している M3508 位置制御ループを探す。"""
        for ctx in self._robots.values():
            for loop in ctx.position_loops:
                if motor_name in loop.motor_names:
                    return loop
        return None

    def _has_motor(self, motor_name: str) -> bool:
        """いずれかのロボットに登録されているモータかどうか。

        「存在しない」と「存在するが PC 側 PID を持たない」を操縦者に区別させるために要る
        (前者は打ち間違い、後者は仕様)。
        """
        return any(motor_name in ctx.can_manager.motors for ctx in self._robots.values())

    # ------------------------------------------------------------------ #
    #  試合状態 (コート / フェーズ / チェックリスト)
    # ------------------------------------------------------------------ #

    async def _handle_set_court(
        self,
        data: dict,
        requester: web.WebSocketResponse | None = None,
    ) -> None:
        raw = data.get("court")
        try:
            court = Court(raw)
        except ValueError:
            # 通らない要求には必ず理由を返す。黙ってログだけ出すと、Monitor は
            # コートを切り替えたつもりのまま逆コートの分岐で試合に入る
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

    async def _handle_match_start(self, requester: web.WebSocketResponse | None = None) -> None:
        if not self.match.match_start():
            await self._reject_command(
                requester,
                "match_start",
                phase_deny_reason("match_start", self.match.phase) or "試合を開始できません",
            )
            return

        # 開始直前にもう一度流し込む (取りこぼすとシーケンスが逆コートの分岐で動く)
        self._apply_court()
        # フェーズを進めるだけで機体は動かさない。動き出すのは各操縦者の sequence_start から
        logger.info("試合開始: court=%s", self.match.court.value)

        await self._broadcast_match_state()

    async def activate_e_stop(self, *, reason: str | None = None) -> None:
        """緊急停止を発動する (操縦者コマンドと内部検知の共通経路)。

        同期監視のような内部の異常検知も、操縦者が押した場合と完全に同じ順序で
        停止させる必要がある (停止経路が 2 つあると片方だけ穴が空く)。
        既に停止中に再度呼ばれても、状態を壊さず停止指令を送り直すだけで済む。

        Args:
            reason: 停止理由。試合中に「なぜ止まったか」が操縦者に伝わらないと
                復旧できないため、ログと WS 配信の両方に載せる。
        """
        logger.warning("緊急停止発動: %s", reason or "操縦者コマンド")
        self._e_stop_active = True
        # 最初に判明した原因を残す。機体側の自動検知で止まった直後に操縦者が
        # E-STOP を押すのは普通の流れで、そこで理由を上書きすると画面の説明が
        # 「機体が検知した原因」から「操縦者が押した」という正反対へ変わる
        if self._e_stop_reason is None:
            self._e_stop_reason = reason
        # 動作確認は runner 登録から run() 開始までのあいだ is_running=False の窓を
        # 持つ。そこを条件にすると起動しかけの動作確認だけが停止をすり抜けるため、
        # 状態を見ずに全 runner へ中断を要求する (run() 側は要求を捨てない)
        for runner in self._motor_check_runners.values():
            runner.abort()
        # ジョグの起点を捨てる。停止中に機構が自重で下がっていた場合、解除後の
        # 1 回目のジョグが古い起点から飛ぶ。停止フレームの送信より前に行うのは、
        # 送信が丸ごと失敗しても必ず捨てさせるため
        for ctx in self._robots.values():
            if ctx.manual is not None:
                ctx.manual.on_e_stop()
        try:
            await self._send_e_stop_frames()
        except Exception:
            # 送信経路が丸ごと壊れても操縦者の WS を落とさない。ここで例外を投げると
            # 接続が切れ、解除操作も緊急停止状態の表示もできなくなる
            logger.exception("E-STOP 停止フレーム送信に失敗")
        finally:
            # 停止フレームの成否に関わらずシーケンスを止める。走らせたままだと
            # 次のステップが新しいモータ目標値を送り、緊急停止を上書きしてしまう
            self._stop_all_sequences(discard_pending_start=True)
            await self._broadcast_e_stop_state()

    async def _send_e_stop_frames(self) -> None:
        """全ロボットの全モータ / 全バスへ停止フレームを送る。

        1 モータ・1 バスの送信失敗で他への送信を諦めないよう個別に握り潰す。
        """
        e_stop_msg = GenericDriver.encode_e_stop()
        for name, ctx in self._robots.items():
            # 最初に M3508 を止める。左右直結の Y 軸は押し合ったまま残ると即座に
            # 機構を壊すうえ、ドライバ固有の停止フレームも 0x7FF も効かない
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
            # 目標を残すと、解除した瞬間に再送が走って操縦者の操作なしに動き出す
            for refresher in ctx.target_refreshers:
                refresher.clear_targets()
            logger.info("E-STOP 送信試行完了: %s", name)

    def _reset_sync_latches(self) -> None:
        """同期ずれのラッチを解除し、監視を再び有効な状態へ戻す。

        解除は「ずれを無かったことにする」操作ではない。位置制御ループ側の
        ラッチは電流 0 を維持し続け、``SyncMonitor`` 側のラッチは同じ軸で二度と
        発報しないという意味を持つため、解除経路が無いままだと「操縦者は復帰した
        つもりで、実際には y_axis が動かず rotate が無監視で回る」状態になる。
        解除後もずれが残っていれば双方が再び検知して緊急停止へ戻すので、
        ここで外して機構の異常が隠れることはない。
        """
        for name, ctx in self._robots.items():
            for loop in ctx.position_loops:
                loop.reset_sync_violation()
            for monitor in ctx.sync_monitors:
                monitor.reset()
            logger.info("同期ずれラッチを解除: robot=%s", name)

    def _safety_state(self, robot_name: str) -> dict[str, object]:
        """安全機構の状態 (ラッチ中の軸 + 保護ループの生死)。

        ラッチ中の軸が分からないと操縦者は復旧手順を選べず、200Hz の位置制御と
        50Hz の同期監視、20Hz の目標値再送が死んだことは配信しない限り誰にも
        気付けない (WS は繋がったままで、モータ状態も届き続けるため画面は正常に
        見える)。目標値再送が死ぬと 500ms 後にファームのウォッチドッグが全 generic
        アクチュエータの出力を落とすため、同じ理由でここに載せる。
        判定は UI 側で組み立て直させずここに一本化する。
        """
        ctx = self._robots[robot_name]
        violations: set[str] = set()
        for loop in ctx.position_loops:
            violations |= set(loop.sync_violations)
        for monitor in ctx.sync_monitors:
            violations |= set(monitor.violated)

        return {
            "sync_violations": sorted(violations),
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

    async def _reactivate_motors(self) -> None:
        """緊急停止解除後にモータの励磁を戻す。

        EDULITE 05 は非常停止で無励磁になるため、解除で再励磁しないと以後の位置指令が
        一切効かない。再励磁自体はドライバ側が現在角を保持目標に書いてから行うので、
        解除操作そのものでロボットが動くことはない。
        """
        for name, ctx in self._robots.items():
            try:
                await ctx.can_manager.activate_motors(should_abort=lambda: self._e_stop_active)
            except Exception:
                logger.exception("緊急停止解除後のモータ有効化に失敗: robot=%s", name)

        # 有効化の途中で再び緊急停止が入ると、中断判定をすり抜けた enable が
        # 停止フレームより後に届きうる。念のため停止フレームを送り直す。
        if self._e_stop_active:
            logger.warning("有効化中に緊急停止が再度入ったため停止フレームを再送します")
            try:
                await self._send_e_stop_frames()
            except Exception:
                logger.exception("E-STOP 停止フレーム再送に失敗")

    def _stop_all_sequences(self, *, discard_pending_start: bool = False) -> None:
        """全ロボットのシーケンスを通常停止する (緊急停止と異なり CAN 層は触らない)。

        discard_pending_start=True では未処理の開始/ジャンプ要求も破棄する。
        緊急停止直前に届いた開始要求が停止処理の直後に発火するのを防ぐため。
        """
        for ctx in self._robots.values():
            self._stop_sequence(ctx, discard_pending_start=discard_pending_start)

    def _stop_sequence(self, ctx: RobotContext, *, discard_pending_start: bool = False) -> None:
        """1 台のシーケンスを通常停止する。**破棄が先、停止が後。**

        逆順にすると、停止処理のあいだに届いた開始要求が破棄をすり抜けて残る。
        順序を 1 箇所に持たないと、呼び出し側 (緊急停止 / 試合終了 / 手動への切替) の
        どれか 1 つだけが書き写しを誤り、そこだけが「止めた直後に動き出す」。
        """
        if discard_pending_start:
            ctx.sequence.discard_pending_start()
        if ctx.sequence.is_running:
            ctx.sequence.request_stop()

    async def _broadcast_match_state(self) -> None:
        await self._broadcast_json(self.match.to_dict())

    async def _reject_command(
        self,
        requester: web.WebSocketResponse | None,
        command: str,
        reason: str,
    ) -> None:
        """拒否理由を要求元 1 台にだけ返す。

        拒否は「今その操作をした人」への返答であって全員への通知ではない。
        全配信すると、Monitor の set_court が試合中に弾かれただけで両操縦者の画面にも
        赤トーストが出る。自分が押していない操作の拒否が混ざると、本当に自分の操作が
        通らなかったときの通知と区別できなくなる。
        要求元が居ない経路 (HTTP POST・内部の安全機構) では誰にも送らない。
        """
        if requester is None or requester.closed:
            return
        msg = json.dumps(
            {"type": "command_rejected", "command": command, "reason": reason},
            ensure_ascii=False,
        )
        if not await self._send_or_drop(requester, msg):
            await self._drop_clients({requester})

    async def _broadcast_e_stop_state(self) -> None:
        """緊急停止の状態と理由を配信する。理由の出所はサーバーの保持値だけ。

        呼び出し側から理由を受け取る形にしていたときは、停止中の定期再配信
        (`_broadcast_state`) が理由なしで呼ぶため、UI に届く最後の 1 通からは
        必ず理由が抜けていた。配信フォーマット自体を lossy にしないため、
        載せる値はここが `_e_stop_reason` から引く。
        """
        payload: dict[str, object] = {"type": "e_stop_state", "active": self._e_stop_active}
        if self._e_stop_reason is not None:
            # 未知フィールドは既存 UI が無視するため、理由が無いときは付けない
            payload["reason"] = self._e_stop_reason
        await self._broadcast_json(payload)

    async def _send_or_drop(self, ws: web.WebSocketResponse, msg: str) -> bool:
        """1 クライアントへ送信する。詰まった相手は生存とみなさず切り離す。

        aiohttp の `send_str` は相手が読まなくなると無期限に待つ。配信は全
        クライアントへ直列に行うため、1 台でも詰まると他の全員 — Monitor を含む —
        のテレメトリが止まる。しかも WebSocket 自体は開いたままなので UI は
        「接続中」を出し続け、操縦者は凍った値を最新だと思って見続けることになる。
        操縦者のノート PC がスリープに入る・Wi-Fi が切れるだけで起きうるため、
        送信ごとに上限を設けて超えた相手を切り離す。
        """
        try:
            await asyncio.wait_for(ws.send_str(msg), timeout=_WS_SEND_TIMEOUT_S)
            return True
        except TimeoutError:
            logger.warning("WebSocket 送信がタイムアウトしたためクライアントを切り離します")
        except ConnectionResetError:
            logger.debug("WebSocket 送信先が既に切断されています")
        except Exception:
            logger.warning("WebSocket 送信に失敗したためクライアントを切り離します", exc_info=True)
        return False

    async def _close_quietly(self, ws: web.WebSocketResponse) -> None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(ws.close(), timeout=_WS_SEND_TIMEOUT_S)

    async def _drop_clients(self, dead: set[web.WebSocketResponse]) -> None:
        """切り離したクライアントの後始末。ソケットも閉じて再接続を促す。

        `close()` は相手からのクローズ応答を待つ。送信が詰まっている相手は
        まさにその応答を返さないので、ここで await すると送信タイムアウトを
        設けた意味がなくなり配信ループが同じ場所で止まる。
        後始末は別タスクへ切り離し、配信ループは決して待たない。
        """
        self._ws_clients -= dead
        for ws in dead:
            task = asyncio.create_task(self._close_quietly(ws))
            self._closing_tasks.add(task)
            task.add_done_callback(self._closing_tasks.discard)

    async def _broadcast_json(self, payload: dict) -> None:
        """1 メッセージを全クライアントへ配信する。"""
        await self._fanout([payload])

    async def _fanout(self, payloads: list[dict]) -> None:
        """全クライアントへの配信経路はここ 1 本だけ。

        配信は全クライアントへ直列に行うため、1 台でも詰まると他の全員 —
        Monitor を含む — のテレメトリが止まる。それを防ぐ約束事 (送信ごとに
        `_send_or_drop` の上限を通す・切り離しの `close()` は別タスクへ逃がす) は
        経路が分かれているぶんだけ守り続ける必要があり、増えた経路で 1 つ抜けた
        だけで同じ事故に戻る。経路を 1 本にして、約束事もここだけで守る。

        シリアライズはクライアント数によらず 1 回。20Hz の配信で全員ぶん
        JSON を作り直すのは無駄でしかない。

        送信はクライアント単位でまとめ、途中で失敗した相手には残りを送らない
        (送れないと分かった相手に投げ続けるぶん、他の配信が遅れる)。

        反復は必ず接続集合のスナップショットに対して行う。送信は 1 通ごとに
        await するため、その隙に `_ws_handler` が同じ集合へ add / discard を
        行いうる (操縦者のリロード 1 回で起きる)。集合をそのまま回すと
        `RuntimeError: Set changed size during iteration` になり、例外ガードの
        無い `activate_e_stop` 経由では E-STOP を押した本人の WS が切れる。
        """
        if not payloads:
            return
        messages = [json.dumps(payload, ensure_ascii=False) for payload in payloads]

        dead: set[web.WebSocketResponse] = set()
        for ws in list(self._ws_clients):
            if ws.closed:
                dead.add(ws)
                continue
            for msg in messages:
                if not await self._send_or_drop(ws, msg):
                    dead.add(ws)
                    break

        await self._drop_clients(dead)

    # ------------------------------------------------------------------ #
    #  アクチュエータ動作確認 (Phase 6 段階⑨ — タスク 6-22)
    # ------------------------------------------------------------------ #

    async def _start_motor_check(self, robot_name: str) -> bool:
        """指定ロボットの動作確認を起動する。拒否時は False を返す。

        拒否条件の優先順:
          1. 試合中 (モータを微小駆動するため試合進行を乱す)
          2. 緊急停止中 (誤発火による微小駆動を完全に止める)
          3. 手動操縦モード (制御権の二重取得を防ぐ)
          4. 通常シーケンス実行中 (制御権の二重取得を防ぐ)
          5. 既に動作確認実行中 (二重起動の防止)

        WS 経由は handle_command でも同じフェーズ判定を行うが、HTTP POST は
        そこを通らないため本メソッド側にもゲートを置く。
        """
        if robot_name not in self._robots:
            return False

        phase_deny = phase_deny_reason("motor_check_start", self.match.phase)
        if phase_deny is not None:
            await self._broadcast_motor_check_error(robot_name, phase_deny)
            return False

        if self._e_stop_active:
            await self._broadcast_motor_check_error(
                robot_name, "緊急停止中のため動作確認を実行できません"
            )
            return False

        ctx = self._robots[robot_name]
        if ctx.mode is OperationMode.MANUAL:
            # 手動は操縦者がいつ軸を動かすか分からない。動作確認は 1 台ずつ順に
            # 駆動していく手順なので、途中で別の指令が割り込むと結果が意味を失う
            await self._broadcast_motor_check_error(
                robot_name, "手動操縦モードのため動作確認を実行できません"
            )
            return False
        if ctx.sequence.is_running:
            await self._broadcast_motor_check_error(
                robot_name, "通常シーケンス実行中のため動作確認を実行できません"
            )
            return False

        # 二重起動は runner の `is_running` ではなく実行タスクの生死で判定する。
        # runner を登録してから `run()` へ入るまでのあいだ `is_running` は False で、
        # そこを素通しすると `_motor_check_runners` が上書きされて 1 台目が誰からも
        # abort できなくなる。さらに pause/resume に入れ子カウントが無いため、
        # 先に終わった側の resume() がもう一方の駆動中に送信を再開させる
        existing_task = self._motor_check_tasks.get(robot_name)
        if existing_task is not None and not existing_task.done():
            await self._broadcast_motor_check_error(robot_name, "既に動作確認を実行中です")
            return False

        # MotorCheckRunner にはロボットに登録された全モータを渡す。順序は登録順
        # = config の宣言順で、指差喚呼の読み上げ順がこれに従う。
        runner = MotorCheckRunner(
            robot_name=robot_name,
            can_manager=ctx.can_manager,
            motors=ctx.can_manager.motors,
            per_motor_timeout_ms=float(self._motor_check_settings["per_motor_timeout_ms"]),
            feedback_timeout_ms=self._health.feedback_timeout_ms,
            default_magnitude=self._motor_check_settings["default_magnitude"],  # type: ignore[arg-type]
            per_motor_overrides=self._motor_check_settings["per_motor_overrides"],  # type: ignore[arg-type]
            # 起動判定を通ってから実際に駆動するまでの窓で停止が入ることがある。
            # M3508PositionLoop / build_motor_group と同じく、走り出した後も
            # 毎ステップ緊急停止を見て駆動を打ち切る
            is_estop_active=lambda: self._e_stop_active,
        )

        # コールバックは runner の同期コンテキストから呼ばれる。WS 配信は async なので
        # asyncio.create_task で fire-and-forget する。
        # GC で task が消失しないよう _bg_tasks セットに保持する (RUF006 対策)。
        loop = asyncio.get_running_loop()
        bg_tasks: set[asyncio.Task[None]] = set()

        def _spawn(coro) -> None:
            t = loop.create_task(coro)
            bg_tasks.add(t)
            t.add_done_callback(bg_tasks.discard)

        def _on_progress(name: str, idx: int, total: int) -> None:
            _spawn(self._broadcast_motor_check_progress(robot_name, name, idx, total))

        def _on_record(record: MotorCheckRecord) -> None:
            _spawn(self._broadcast_motor_check_record(robot_name, record))

        runner.set_on_progress(_on_progress)
        runner.set_on_record(_on_record)

        self._motor_check_runners[robot_name] = runner

        # 動作確認はモータへ自前の指令を出すため、周期的に指令を出している側と
        # 指令を奪い合う。M3508 位置制御ループとは C620 の電流指令フレーム (0x200) を、
        # 目標値再送とは同じモータの SET_TARGET を奪い合うので、どちらも黙らせて
        # 排他を取る。対象が居ないロボットでは空リストになる。
        pausables: list[M3508PositionLoop | GenericTargetRefresher] = [
            *ctx.position_loops,
            *ctx.target_refreshers,
        ]

        async def _run() -> None:
            try:
                for pausable in pausables:
                    await pausable.pause(reason=f"{robot_name} 動作確認")
                if self._e_stop_active:
                    # pause() は送信中の 1 周期ぶんブロックしうる。その窓のあいだに
                    # 緊急停止が入ったら 1 台も駆動せずに降りる (起動判定は既に
                    # 過去のもので、今この瞬間モータを動かしてよいかを答えていない)
                    await self._broadcast_motor_check_error(
                        robot_name, "緊急停止中のため動作確認を中止しました"
                    )
                    return
                snapshot = await runner.run()
                self._motor_check_last[robot_name] = snapshot
                await self._broadcast_motor_check_done(robot_name, snapshot)
            except Exception as exc:  # pragma: no cover - 防御的
                logger.exception("動作確認エラー (%s): %s", robot_name, exc)
                await self._broadcast_motor_check_error(robot_name, str(exc))
            finally:
                # 中断・例外・キャンセルのいずれで抜けても必ず復帰させる。
                # 止まったままだと昇降軸が保持電流を失って落下し、
                # 再送が止まったままだとコンベアが 500ms で動かなくなる
                for pausable in pausables:
                    pausable.resume()
                self._motor_check_tasks.pop(robot_name, None)
                # runners からは敢えて消さない: GET /motor_check/last の補助情報として
                # 直近 runner を参照したい場合に備える。次回 start で上書きされる。

        task = asyncio.create_task(_run())
        self._motor_check_tasks[robot_name] = task
        return True

    async def _broadcast_motor_check_progress(
        self,
        robot: str,
        current: str,
        index: int,
        total: int,
    ) -> None:
        await self._broadcast_json(
            {
                "type": "motor_check_progress",
                "robot": robot,
                "current": current,
                "index": index,
                "total": total,
            }
        )

    async def _broadcast_motor_check_record(self, robot: str, record: MotorCheckRecord) -> None:
        await self._broadcast_json(
            {
                "type": "motor_check_record",
                "robot": robot,
                "record": record.to_dict(),
            }
        )

    async def _broadcast_motor_check_done(self, robot: str, snapshot: CheckRunSnapshot) -> None:
        await self._broadcast_json(
            {
                "type": "motor_check_done",
                "robot": robot,
                "snapshot": snapshot.to_dict(),
            }
        )

    async def _broadcast_motor_check_error(self, robot: str, message: str) -> None:
        await self._broadcast_json(
            {
                "type": "motor_check_error",
                "robot": robot,
                "message": message,
            }
        )

    async def _motor_check_post(self, request: web.Request) -> web.Response:
        """POST /robots/{robot}/motor_check: 動作確認の起動エンドポイント。

        起動成功時は即時 200 を返し、結果は WS 経由で配信する。拒否時は 409 を返し
        WS 側にもエラーイベントが流れる (両系統の購読側に通知)。
        """
        robot = request.match_info["robot"]
        if robot not in self._robots:
            return web.json_response({"error": "robot not found"}, status=404)

        started = await self._start_motor_check(robot)
        if not started:
            return web.json_response({"started": False, "reason": "拒否"}, status=409)
        return web.json_response({"started": True}, status=200)

    async def _motor_check_get_last(self, request: web.Request) -> web.Response:
        """GET /robots/{robot}/motor_check/last: 直近の動作確認スナップショット。"""
        robot = request.match_info["robot"]
        if robot not in self._robots:
            return web.json_response({"error": "robot not found"}, status=404)

        snapshot = self._motor_check_last.get(robot)
        if snapshot is None:
            return web.json_response({"snapshot": None}, status=200)
        return web.json_response({"snapshot": snapshot.to_dict()}, status=200)

    async def _broadcast_loop(self) -> None:
        """テレメトリ配信ループ。1 回の例外でループごと終わらせてはならない。

        このタスクが死ぬと WebSocket は繋がったままなので、UI は「接続中」を出しつつ
        値だけが凍る。操縦者は画面が生きていると信じたまま古い値を見続けることになり、
        機体が異常でも気付けない。1 フレームの失敗は握って次の周期へ進み、
        配信そのものは何があっても継続させる。
        """
        while True:
            try:
                await self._broadcast_state()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("状態配信に失敗しました (配信は継続します)")
            await asyncio.sleep(self._broadcast_interval)

    def _compute_health(self, robot_name: str) -> HealthSnapshot:
        """指定ロボットの CANManager から HealthSnapshot を組み立てる。

        計算そのものが失敗したときは必ず DOWN に倒す。ここで OK を返すと、
        健全性判定が壊れた瞬間に画面と /health が「正常」を主張し、監視系も
        操縦者も異常を検出する手段を丸ごと失う (異常の有無が分からない状態は
        安全側では「異常」であって「正常」ではない)。
        """
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
        """健全性を判定できなかったことを表すスナップショット。

        バス・モータの一覧は空のままにする。判定できていない以上、個々の状態を
        でっち上げる方が誤解を招く。overall と detail だけで「判定不能」を伝える。
        """
        return HealthSnapshot(timestamp=time.time(), overall=BusHealth.DOWN, detail=detail)

    def _diff_health(
        self,
        robot_name: str,
        prev: HealthSnapshot | None,
        curr: HealthSnapshot,
    ) -> list[dict]:
        """前回スナップショットとの差分から health_change イベントの一覧を生成する。

        前回 None (初回) の場合は空リストを返す。バス・モータそれぞれの state が
        変化したペアだけイベント化する。

        ``robot`` フィールドは必須。Monitor は 2 機分のイベントを 1 本のリストへ
        並べるため、どちらの機体の異常かがイベント自身に載っていないと区別できない
        (バス名は両機で共有しており、target だけでは機体を特定できない)。
        """
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
        """基板側が報告している緊急停止 (FEEDBACK の緊急停止ビット) を探す。

        自作モタドラは物理停止スイッチの押下・CAN の初期化失敗を緊急停止ラッチに
        落とし、緊急停止ビットで報告してくる。サーバーがこれを拾わないと、**機体は止まって
        いるのに UI は平常のまま**になり、操縦者はシーケンスが進まない理由を
        画面から知る術がない (実際に押されたスイッチを探し回ることになる)。

        判定はフィードバックが `_board_e_stop_ignore_before` より後に届いたものに
        限る。解除直後にサーバーが自分で停止をかけ直す経路を作らないため。
        """
        if self._e_stop_active:
            # 既に停止中なら報告するまでもない。ここで再発動すると停止理由が
            # 「最初に判明したもの」から基板の報告へ塗り替わりかねない
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
        # 1) 各ロボットの health を計算 (クライアント不在でも遷移検出のため必ず実行)
        snapshots: dict[str, HealthSnapshot] = {}
        for robot_name in self._robots:
            snapshots[robot_name] = self._compute_health(robot_name)

        # 2) 基板側の緊急停止をサーバー全体へ伝播する。クライアント不在でも必ず行う。
        #    誰も見ていないから止めなくてよい、という理屈は成り立たない
        board_e_stop = self._detect_board_e_stop(snapshots)
        if board_e_stop is not None:
            await self.activate_e_stop(reason=board_e_stop)

        if not self._ws_clients:
            # クライアントがいなくても _last_health は更新する。
            # こうしないと最初のクライアント接続直後に「過去の状態 → 現在」の
            # 巨大な差分が一気に降ってきてしまう。
            self._last_health = snapshots
            return

        # 3) state メッセージ (health 同梱) を生成
        state_messages: list[dict] = []
        change_events: list[dict] = []
        for robot_name, snap in snapshots.items():
            state_messages.append(self._build_state_message(robot_name, snapshot=snap))

            # 4) health_change イベントを差分から生成
            prev = self._last_health.get(robot_name)
            change_events.extend(self._diff_health(robot_name, prev, snap))

        await self._fanout([*state_messages, *change_events])

        # 5) 差分検出後にスナップショットを更新する。順序を逆にすると
        #    1 回目の broadcast で health_change が出てしまう。
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
                # dry-run: 実機フィードバックがないので、UI デモ向けに擬似値を生成
                motors[motor_name] = self._dry_run_motor_state(robot_name, motor_name)
            else:
                s = motor.state
                motors[motor_name] = {
                    "pos": s.position,
                    "vel": s.velocity,
                    "torque": s.current,
                    "temp": s.temperature,
                }
            # ゲインはテレメトリではなく構成情報なので dry-run 分岐の外で足す。
            # 擬似値を作る意味が無く、中に入れると dry-run で全モータが
            # 「調整不可」になって机上で UI を確かめられない
            motors[motor_name]["pid"] = self._motor_pid_state(motor_name)

        # snapshot が未指定 (テストや単独呼び出し) の場合はその場で計算する。
        # _broadcast_state からの呼び出しは事前計算済みのものを使い回して二重計算を避ける。
        if snapshot is None:
            snapshot = self._compute_health(robot_name)

        snapshot_dict = snapshot.to_dict()
        if self._dry_run:
            snapshot_dict = self._dry_run_patch_health(snapshot_dict)

        return {
            "type": "state",
            "robot": robot_name,
            "sequence": progress["sequence"],
            "current_step": progress["current_step"],
            "step_index": progress["step_index"],
            "total_steps": progress["total_steps"],
            "waiting_trigger": progress["waiting_trigger"],
            # UI に step_index/total_steps から実行状態を推測させないため、
            # シーケンス側が持っている実行フラグをそのまま配信する
            "running": progress["running"],
            "steps": progress.get("steps", []),
            "motors": motors,
            "e_stop_active": self.e_stop_active,
            "health": snapshot_dict,
            "safety": self._safety_state(robot_name),
            "manual": self._manual_state(robot_name),
        }

    def _manual_state(self, robot_name: str) -> dict:
        """操作モードと手動操縦の軸一覧。

        軸定義 (可動範囲・プリセット名) は静的だが ``steps`` と同じく state に載せる。
        UI に軸名も可動範囲もハードコードさせないためで、機構が変わって軸が増減しても
        UI 側の変更は要らない。現在値だけがテレメトリなので配信周期はそちらに合わせる。
        """
        ctx = self._robots[robot_name]
        return {
            "mode": ctx.mode.value,
            "axes": ctx.manual.axes_info() if ctx.manual is not None else [],
        }

    def _dry_run_motor_state(self, robot_name: str, motor_name: str) -> dict:
        """dry-run で UI に意味のある動きを見せるための擬似モータ状態。

        ロボット名・モータ名の文字列ハッシュをオフセットに使い、各モータが
        異なる位相で揺らぐようにしている。実値ではなく見栄え重視。
        """
        h = sum(ord(c) for c in robot_name + ":" + motor_name)
        t = time.time()
        return {
            "pos": math.sin(t * 0.6 + h * 0.3) * 1500.0,
            "vel": math.cos(t * 0.9 + h * 0.5) * 80.0,
            "torque": math.sin(t * 0.7 + h * 0.2) * 0.35,
            "temp": 30.0 + math.sin(t * 0.15 + h * 0.7) * 6.0,
        }

    def _dry_run_patch_health(self, snapshot_dict: dict) -> dict:
        """dry-run 時にヘルススナップショットを「全体 OK」相当に整える。

        virtual バスはフィードバックを返さないため通常モータが STALE になるが、
        UI デモ目的で OK 表示にする。実機モードでは呼ばれない。
        """
        snapshot_dict["overall"] = "ok"
        for motor in snapshot_dict.get("motors", []):
            motor["state"] = "ok"
            motor["feedback_age_ms"] = 0
            motor["last_feedback_at"] = time.time()
            motor["detail"] = None
        for bus in snapshot_dict.get("buses", []):
            bus["state"] = "ok"
        return snapshot_dict

    async def start(self) -> None:
        app = self.create_app()
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self._host, self._port)
        await site.start()
        logger.info("サーバー起動: http://%s:%d", self._host, self._port)

        try:
            await asyncio.Event().wait()
        finally:
            await runner.cleanup()

    async def cleanup(self) -> None:
        await self._close_all_clients()
