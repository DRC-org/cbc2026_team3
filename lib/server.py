from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import pathlib
import time
from dataclasses import dataclass, field

from aiohttp import WSMsgType, web

from lib.can_manager import CANManager
from lib.control.position_loop import M3508PositionLoop
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
from lib.match_state import ChecklistItem, Court, MatchState
from lib.motor_check import MotorCheckRunner
from lib.sequence.engine import Sequence

logger = logging.getLogger(__name__)

_WEB_DIST_DIR = pathlib.Path(__file__).resolve().parent.parent / "web" / "dist"

#: 1 クライアントへの送信を諦めるまでの秒数。
#: テレメトリは 20Hz なので、1 秒返ってこない相手は既に落ちているとみなしてよい。
_WS_SEND_TIMEOUT_S = 1.0


# overall を最悪値で集約するためのランク。lib.health._BUS_SEVERITY_RANK と一致させる
# (重複定義を避けたいが、health.py 側を private 扱いにしているため局所コピーする)。
_BUS_SEVERITY_RANK: dict[BusHealth, int] = {
    BusHealth.OK: 0,
    BusHealth.DEGRADED: 1,
    BusHealth.DOWN: 2,
}


#: 緊急停止中に拒否するコマンドと、操縦者へ返す理由。
#: 停止方向の操作 (sequence_stop / e_stop / match_reset / match_finish / e_stop_release) は
#: 緊急停止中こそ通す必要があるため決して載せてはならない。motor_check_start は
#: _start_motor_check が motor_check_error で拒否する別経路を持つのでここでは扱わない。
#: match_start は試合フェーズへ入る操作で、通ると操縦者の sequence_start が解禁される。
#: 緊急停止中に MATCH へ入れてしまうと動作確認もコート設定も同時に閉じてしまうため塞ぐ。
_E_STOP_DENY_MESSAGES: dict[str, str] = {
    "sequence_start": "緊急停止中のためシーケンスを開始できません",
    "sequence_jump": "緊急停止中のためステップ移動できません",
    "trigger": "緊急停止中のためトリガーを送れません",
    "match_start": "緊急停止中のため試合を開始できません",
    "set_param": "緊急停止中のためパラメータを変更できません",
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


class RobotServer:
    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8080,
        *,
        feedback_timeout_ms: float = 500.0,
        temp_warning_c: float = 65.0,
        temp_critical_c: float = 80.0,
        tx_error_threshold: int = 96,
        motor_check_per_motor_timeout_ms: float = 1500.0,
        motor_check_default_magnitude: dict[str, float] | None = None,
        motor_check_per_motor_overrides: dict[str, dict[str, float]] | None = None,
        checklist_definitions: dict[str, list[ChecklistItem]] | None = None,
        dry_run: bool = False,
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
        # dry-run 時はモータ状態とヘルスを擬似的に揺らがせて Web UI の描画を成立させる。
        # 実機運用時は False のまま影響しない。
        self._dry_run: bool = dry_run
        self._sequence_tasks: dict[str, asyncio.Task[None]] = {}

        # 試合全体の状態 (コート / フェーズ / チェックリスト)。
        # 操縦者 2 名 + Monitor が別ブラウザで接続するため正はサーバー側に置く。
        self.match = MatchState(definitions=checklist_definitions)

        # ヘルスチェックしきい値は config/*.yaml の health セクション由来 (Phase 6 段階⑤で反映)
        self._health_thresholds: dict[str, float | int] = {
            "feedback_timeout_ms": feedback_timeout_ms,
            "temp_warning_c": temp_warning_c,
            "temp_critical_c": temp_critical_c,
            "tx_error_threshold": tx_error_threshold,
        }
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
    ) -> None:
        self._robots[name] = RobotContext(
            sequence=sequence,
            can_manager=can_manager,
            position_loops=list(position_loops or []),
        )
        sequence.set_court(self.match.court)

    def _apply_court(self) -> None:
        for ctx in self._robots.values():
            ctx.sequence.set_court(self.match.court)

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
        # 各ロボットのシーケンス実行ループを起動。停止/ジャンプで再起動可能な
        # 永続タスクとして保持し、shutdown でキャンセルする。
        for robot_name in self._robots:
            self._sequence_tasks[robot_name] = asyncio.create_task(
                self._run_sequence_loop(robot_name)
            )

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

        for ws in set(self._ws_clients):
            await ws.close()
        self._ws_clients.clear()

    async def _run_sequence_loop(self, robot_name: str) -> None:
        """シーケンスを永続的に走らせる。停止/完走後は resume 要求を待つ。

        起動時は resume を立てない。操縦者の明示的な開始合図 (sequence_start) が
        あるまでロボットを動かしてはならない。
        """
        seq = self._robots[robot_name].sequence
        while True:
            await seq._resume_event.wait()
            seq._resume_event.clear()
            try:
                await seq.run()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("シーケンス実行中にエラー: %s", robot_name)
            # 通常停止された場合はステップを 0 に戻して次の起動を待つ。
            # 完走 (current_index == total) はそのまま位置を保持する。
            if seq._stop_event.is_set():
                seq._current_index = 0
                seq._stop_event.clear()

    async def _ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._ws_clients.add(ws)
        logger.info("WebSocket 接続: %s", request.remote)

        # match_state は変化時のみ配信するため、接続直後に一度スナップショットを送る。
        # これがないとリロード直後のクライアントが現在のモード/フェーズを知れない。
        try:
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
                    await self._handle_command(data)
                elif msg.type == WSMsgType.ERROR:
                    logger.error("WebSocket エラー: %s", ws.exception())
        finally:
            self._ws_clients.discard(ws)
            logger.info("WebSocket 切断: %s", request.remote)

        return ws

    async def _handle_command(self, data: dict) -> None:
        cmd_type = data.get("type")

        # フェーズゲート。UI でボタンを隠すだけでは WS 直叩きやリロード直後を防げない。
        if isinstance(cmd_type, str):
            deny = self.match.deny_reason(cmd_type)
            if deny is not None:
                logger.info("コマンド拒否: %s (%s)", cmd_type, deny)
                if cmd_type == "motor_check_start":
                    # 動作確認の拒否は既存の専用イベントに合わせる (UI 側の表示経路が別)
                    robot_name = data.get("robot")
                    await self._broadcast_motor_check_error(str(robot_name), deny)
                else:
                    await self._broadcast_command_rejected(cmd_type, deny)
                return

            # 緊急停止ゲート。フェーズが MATCH のままだと緊急停止中でも START が通り、
            # シーケンスが実モータ指令を書き込んで停止指令を上書きしてしまう。
            # match_start は READY で受理されうるので、フェーズ遷移前にここで止める。
            if self._e_stop_active and cmd_type in _E_STOP_DENY_MESSAGES:
                reason = _E_STOP_DENY_MESSAGES[cmd_type]
                logger.info("コマンド拒否: %s (%s)", cmd_type, reason)
                await self._broadcast_command_rejected(cmd_type, reason)
                return

        if cmd_type == "trigger":
            robot_name = data.get("robot")
            if robot_name and robot_name in self._robots:
                self._robots[robot_name].sequence.trigger()
                logger.info("trigger: %s", robot_name)

        elif cmd_type == "e_stop":
            await self.activate_e_stop()

        elif cmd_type == "e_stop_release":
            logger.info("緊急停止解除コマンド受信")
            self._e_stop_active = False
            await self._broadcast_e_stop_state()
            await self._reactivate_motors()

        elif cmd_type == "health_check":
            # クライアントからの即時ヘルス要求。次回ループを待たずに即配信する。
            await self._broadcast_state()

        elif cmd_type == "set_param":
            motor_name = data.get("motor")
            key = data.get("key")
            value = data.get("value")
            logger.info("set_param: motor=%s key=%s value=%s", motor_name, key, value)

        elif cmd_type == "sequence_jump":
            robot_name = data.get("robot")
            step_index = data.get("step_index")
            if robot_name and robot_name in self._robots and isinstance(step_index, int):
                self._robots[robot_name].sequence.request_jump(step_index)
                logger.info("sequence_jump: %s -> %d", robot_name, step_index)

        elif cmd_type == "sequence_stop":
            robot_name = data.get("robot")
            if robot_name and robot_name in self._robots:
                self._robots[robot_name].sequence.request_stop()
                logger.info("sequence_stop: %s", robot_name)

        elif cmd_type == "sequence_start":
            robot_name = data.get("robot")
            if robot_name and robot_name in self._robots:
                self._robots[robot_name].sequence.request_start()
                logger.info("sequence_start: %s", robot_name)

        elif cmd_type == "motor_check_start":
            robot_name = data.get("robot")
            # 知らないロボットは silent ignore (ws を切断しないため)
            if robot_name and robot_name in self._robots:
                await self._start_motor_check(robot_name)

        elif cmd_type == "motor_check_abort":
            robot_name = data.get("robot")
            if robot_name and robot_name in self._motor_check_runners:
                self._motor_check_runners[robot_name].abort()

        elif cmd_type == "set_court":
            await self._handle_set_court(data)

        elif cmd_type == "checklist_set":
            role = data.get("role")
            item_id = data.get("item_id")
            checked = bool(data.get("checked"))
            if isinstance(role, str) and isinstance(item_id, str):
                if self.match.set_checklist_item(role, item_id, checked):
                    await self._broadcast_match_state()
                else:
                    logger.warning("未知のチェック項目: role=%s item=%s", role, item_id)

        elif cmd_type == "checklist_reset":
            role = data.get("role")
            self.match.reset_checklist(role if isinstance(role, str) else None)
            await self._broadcast_match_state()

        elif cmd_type == "match_start":
            await self._handle_match_start()

        elif cmd_type == "match_finish":
            if self.match.match_finish():
                logger.info("試合終了")
                self._stop_all_sequences()
                await self._broadcast_match_state()

        elif cmd_type == "match_reset":
            self.match.match_reset()
            logger.info("セッティングタイムへ復帰")
            self._stop_all_sequences()
            self._apply_court()
            await self._broadcast_match_state()

        else:
            logger.debug("未知のコマンド: %s", cmd_type)

    # ------------------------------------------------------------------ #
    #  試合状態 (コート / フェーズ / チェックリスト)
    # ------------------------------------------------------------------ #

    async def _handle_set_court(self, data: dict) -> None:
        raw = data.get("court")
        try:
            court = Court(raw)
        except ValueError:
            logger.warning("未知のコート: %s", raw)
            return
        if not self.match.set_court(court):
            return
        self._apply_court()
        logger.info("コート変更: %s", court.value)
        await self._broadcast_match_state()

    async def _handle_match_start(self) -> None:
        if not self.match.match_start():
            await self._broadcast_command_rejected(
                "match_start", self.match.deny_reason("match_start") or "試合を開始できません"
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
        for runner in self._motor_check_runners.values():
            if runner.is_running:
                runner.abort()
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
            await self._broadcast_e_stop_state(reason=reason)

    async def _send_e_stop_frames(self) -> None:
        """全ロボットの全モータ / 全バスへ停止フレームを送る。

        1 モータ・1 バスの送信失敗で他への送信を諦めないよう個別に握り潰す。
        """
        e_stop_msg = GenericDriver.encode_e_stop()
        for name, ctx in self._robots.items():
            for motor_name, motor in ctx.can_manager._motors.items():
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
            for bus_name in ctx.can_manager._buses:
                try:
                    await ctx.can_manager.send_to_bus(bus_name, e_stop_msg)
                except Exception:
                    logger.exception(
                        "E-STOP bus送信失敗: robot=%s bus=%s",
                        name,
                        bus_name,
                    )
            logger.info("E-STOP 送信試行完了: %s", name)

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
            if discard_pending_start:
                ctx.sequence._resume_event.clear()
                ctx.sequence._jump_request = None
            if ctx.sequence._running:
                ctx.sequence.request_stop()

    async def _broadcast_match_state(self) -> None:
        await self._broadcast_json(self.match.to_dict())

    async def _broadcast_command_rejected(self, command: str, reason: str) -> None:
        await self._broadcast_json(
            {"type": "command_rejected", "command": command, "reason": reason}
        )

    async def _broadcast_e_stop_state(self, *, reason: str | None = None) -> None:
        payload: dict[str, object] = {"type": "e_stop_state", "active": self._e_stop_active}
        if reason is not None:
            # 未知フィールドは既存 UI が無視するため、理由が無いときは付けない
            payload["reason"] = reason
        msg = json.dumps(payload, ensure_ascii=False)
        dead: set[web.WebSocketResponse] = set()
        for ws in self._ws_clients:
            if ws.closed or not await self._send_or_drop(ws, msg):
                dead.add(ws)
        await self._drop_clients(dead)

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
        """単一の JSON dict を全クライアントへ送信する共通ヘルパ。

        既存 _broadcast_state / _broadcast_e_stop_state はそのまま残し、
        本メソッドは動作確認イベント (motor_check_*) 専用に使う。
        """
        msg = json.dumps(payload, ensure_ascii=False)
        dead: set[web.WebSocketResponse] = set()
        for ws in self._ws_clients:
            if ws.closed or not await self._send_or_drop(ws, msg):
                dead.add(ws)
        await self._drop_clients(dead)

    # ------------------------------------------------------------------ #
    #  アクチュエータ動作確認 (Phase 6 段階⑨ — タスク 6-22)
    # ------------------------------------------------------------------ #

    async def _start_motor_check(self, robot_name: str) -> bool:
        """指定ロボットの動作確認を起動する。拒否時は False を返す。

        拒否条件の優先順:
          1. 試合中 (モータを微小駆動するため試合進行を乱す)
          2. 緊急停止中 (誤発火による微小駆動を完全に止める)
          3. 通常シーケンス実行中 (制御権の二重取得を防ぐ)
          4. 既に動作確認実行中 (二重起動の防止)

        WS 経由は _handle_command でも同じフェーズ判定を行うが、HTTP POST は
        そこを通らないため本メソッド側にもゲートを置く。
        """
        if robot_name not in self._robots:
            return False

        phase_deny = self.match.deny_reason("motor_check_start")
        if phase_deny is not None:
            await self._broadcast_motor_check_error(robot_name, phase_deny)
            return False

        if self._e_stop_active:
            await self._broadcast_motor_check_error(
                robot_name, "緊急停止中のため動作確認を実行できません"
            )
            return False

        ctx = self._robots[robot_name]
        if ctx.sequence._running:
            await self._broadcast_motor_check_error(
                robot_name, "通常シーケンス実行中のため動作確認を実行できません"
            )
            return False

        existing = self._motor_check_runners.get(robot_name)
        if existing is not None and existing.is_running:
            await self._broadcast_motor_check_error(robot_name, "既に動作確認を実行中です")
            return False

        # MotorCheckRunner にはロボットに登録された全モータを渡す。順序は dict 挿入順
        # = config の宣言順を保つため OrderedDict 的な扱いは Python 3.7+ で保証されている。
        motors = ctx.can_manager._motors
        runner = MotorCheckRunner(
            robot_name=robot_name,
            can_manager=ctx.can_manager,
            motors=motors,
            per_motor_timeout_ms=float(self._motor_check_settings["per_motor_timeout_ms"]),
            feedback_freshness_ms=float(self._health_thresholds["feedback_timeout_ms"]),
            default_magnitude=self._motor_check_settings["default_magnitude"],  # type: ignore[arg-type]
            per_motor_overrides=self._motor_check_settings["per_motor_overrides"],  # type: ignore[arg-type]
        )

        # コールバックは runner の同期コンテキストから呼ばれる。WS 配信は async なので
        # asyncio.create_task で fire-and-forget する。loop は run() 中なので必ず取れる。
        # GC で task が消失しないよう _bg_tasks セットに保持する (RUF006 対策)。
        loop = asyncio.get_event_loop()
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

        # 動作確認は C620 の電流指令フレーム (0x200) を自前で送るため、同一バスの
        # 位置制御ループが走っていると互いのフレームを上書きし合う。ループ側を
        # 黙らせて排他を取る。M3508 が居ないロボットでは空リストになる。
        loops = list(ctx.position_loops)

        async def _run() -> None:
            try:
                for loop in loops:
                    await loop.pause(reason=f"{robot_name} 動作確認")
                snapshot = await runner.run()
                self._motor_check_last[robot_name] = snapshot
                await self._broadcast_motor_check_done(robot_name, snapshot)
            except Exception as exc:  # pragma: no cover - 防御的
                logger.exception("動作確認エラー (%s): %s", robot_name, exc)
                await self._broadcast_motor_check_error(robot_name, str(exc))
            finally:
                # 中断・例外・キャンセルのいずれで抜けても必ず復帰させる。
                # 止まったままだと昇降軸が保持電流を失って落下する
                for loop in loops:
                    loop.resume()
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

        テスト等で MagicMock(spec=CANManager) を渡された場合 health() が
        HealthSnapshot を返さないため、防御的に空スナップショットへフォールバックする。
        """
        ctx = self._robots[robot_name]
        try:
            snap = ctx.can_manager.health(
                feedback_timeout_ms=float(self._health_thresholds["feedback_timeout_ms"]),
                temp_warning_c=float(self._health_thresholds["temp_warning_c"]),
                temp_critical_c=float(self._health_thresholds["temp_critical_c"]),
                tx_error_threshold=int(self._health_thresholds["tx_error_threshold"]),
            )
        except Exception:  # pragma: no cover - 防御的フォールバック
            snap = None

        if not isinstance(snap, HealthSnapshot):
            return HealthSnapshot(timestamp=time.time(), overall=BusHealth.OK)
        return snap

    def _diff_health(
        self,
        robot_name: str,
        prev: HealthSnapshot | None,
        curr: HealthSnapshot,
    ) -> list[dict]:
        """前回スナップショットとの差分から health_change イベントの一覧を生成する。

        前回 None (初回) の場合は空リストを返す。バス・モータそれぞれの state が
        変化したペアだけイベント化する。robot_name はターゲット文字列に含めない
        (現状クライアントはバス/モータ名のみで識別できるため)。
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
                        "level": _level_for_motor_state(m.state),
                        "target": f"motor:{m.name}",
                        "from": old_m.state.value,
                        "to": m.state.value,
                        "message": f"motor {m.name} {old_m.state.value}→{m.state.value}",
                    }
                )

        return events

    async def _broadcast_state(self) -> None:
        # 1) 各ロボットの health を計算 (クライアント不在でも遷移検出のため必ず実行)
        snapshots: dict[str, HealthSnapshot] = {}
        for robot_name in self._robots:
            snapshots[robot_name] = self._compute_health(robot_name)

        if not self._ws_clients:
            # クライアントがいなくても _last_health は更新する。
            # こうしないと最初のクライアント接続直後に「過去の状態 → 現在」の
            # 巨大な差分が一気に降ってきてしまう。
            self._last_health = snapshots
            return

        # 2) state メッセージ (health 同梱) を生成
        state_messages: list[str] = []
        change_events: list[str] = []
        for robot_name, snap in snapshots.items():
            state = self._build_state_message(robot_name, snapshot=snap)
            state_messages.append(json.dumps(state, ensure_ascii=False))

            # 3) health_change イベントを差分から生成
            prev = self._last_health.get(robot_name)
            for ev in self._diff_health(robot_name, prev, snap):
                change_events.append(json.dumps(ev, ensure_ascii=False))

        dead: set[web.WebSocketResponse] = set()
        for ws in self._ws_clients:
            if ws.closed:
                dead.add(ws)
                continue
            for msg_text in (*state_messages, *change_events):
                if not await self._send_or_drop(ws, msg_text):
                    dead.add(ws)
                    break

        await self._drop_clients(dead)

        # 4) 差分検出後にスナップショットを更新する。順序を逆にすると
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
        for motor_name, motor in ctx.can_manager._motors.items():
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
            "steps": progress.get("steps", []),
            "motors": motors,
            "e_stop_active": self.e_stop_active,
            "health": snapshot_dict,
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
        for ws in set(self._ws_clients):
            await ws.close()
        self._ws_clients.clear()
