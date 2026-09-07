from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import pathlib
import time
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

#: 1 クライアントへの送信を諦めるまでの秒数。
#: テレメトリは 20Hz なので、1 秒返ってこない相手は既に落ちているとみなしてよい。

#: 「励磁されているはず」の起点から、無励磁を異常として報告し始めるまでの猶予。
#: enable を送ってから次のフィードバックが届くまでに 1 周期ぶんの窓がある。
#: DM3520 の再送は 20Hz (50ms) なので、その 10 倍を取れば偽報告は出ない。
_ENERGIZE_GRACE_S = 0.5

#: サーバー起動から、自作モタドラの `INFO` 未受信を「未確認」として報告し始めるまでの
#: 猶予。`INFO` は 1Hz (仕様書 §3.4) なので、起動直後の空白は正常。位相のずれで
#: 最悪 1 周期分待たされてもなお埋まるよう、余裕を持って 3 秒 (3 周期分) を取る。
_FIRMWARE_INFO_GRACE_S = 3.0

#: 緊急停止解除が、進行中の単発再励磁タスクを畳むのに待つ上限。
#: **キャンセルが効いていれば 1 周期で終わる値である** —— ここまで掛かるのは
#: エグゼキュータへ入った `bus.send` のように、キャンセルしても止まらない
#: ブロッキング呼び出しに入っているときだけ。詳細は `_settle_pending_reenergize`。
_REENERGIZE_CANCEL_TIMEOUT_S = 0.5

#: 拒否通知の宛先。HTTP POST や内部の安全機構からの呼び出しには返す相手が居ない。
type WSOrNone = web.WebSocketResponse | None


def _measured_only(
    values: dict[str, float], telemetry: TelemetrySupport
) -> dict[str, float | None]:
    """測る手段の無い項目を ``None`` へ倒す。**実機と dry-run が通る唯一の関門。**

    DC 基板・電磁弁基板は電流も温度も速度も測れず、位置すら持たない
    (仕様書 §3.2)。``MotorState`` は制御経路の都合で float 固定なので、そこには
    0.0 が入ったまま流れてくる。それを配信へ素通しすると UI には
    「測ったように見える 0」が出て、操縦者は「本当に 0」なのか
    「そもそも測っていない」のかを区別できない。

    可否を決めるのはドライバの `TelemetrySupport` だけで、ここは倒す場所に徹する。
    ドライバ種別による分岐をここへ (まして UI へ) 書き写すと、ドライバを足した人が
    配信側の表を直し忘れる形で 0 が復活する。
    """
    return {
        "pos": values["pos"] if telemetry.position else None,
        "vel": values["vel"] if telemetry.velocity else None,
        "torque": values["torque"] if telemetry.current else None,
        "temp": values["temp"] if telemetry.temperature else None,
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
    # そのロボットの M3508 位置制御ループ (バスごと 1 本)。**動作確認中も回し続ける**
    # (理由は _motor_check_pausables)
    position_loops: list[M3508PositionLoop] = field(default_factory=list)
    # そのロボットの同期監視。ラッチの解除経路がサーバー側に無いと、一度ずれを
    # 検知した軸は二度と発報せず、操縦者は無監視のまま機体を動かすことになる
    sync_monitors: list[SyncMonitor] = field(default_factory=list)
    # 自作モタドラのウォッチドッグ対策と、問い合わせ駆動 2 種の生存問い合わせ。
    # **動作確認中も回し続ける** (理由は _motor_check_pausables)。
    # 緊急停止時だけは保持した目標を捨てる (解除だけで動き出させない)
    target_refreshers: list[TargetRefresher] = field(default_factory=list)
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
        checklist_definitions: dict[str, list[ChecklistItem]] | None = None,
        match_settings: MatchSettings = DEFAULT_MATCH,
        dry_run: bool = False,
        dev_tools: bool = False,
    ) -> None:
        self._host = host
        self._port = port
        self._app: web.Application | None = None
        self._robots: dict[str, RobotContext] = {}
        #: WS クライアント集合と唯一の配信経路 (lib/ws_hub.py)。
        #: 送信上限・切り離しの別タスク化・集合のスナップショットという 4 つの約束は
        #: あちらに閉じており、配信経路を増やすには WsHub へメソッドを足すしかない
        self._ws = WsHub()
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
        # 「この時刻以降は全モータが励磁されているはず」。起動直後と緊急停止解除の
        # 直後に置き、緊急停止中は None にする。無励磁の報告をこの猶予つきで行うのは、
        # enable を送ってから次のフィードバックが届くまでに 1 周期ぶんの窓があり、
        # そこを無条件に異常とすると解除のたびに偽の警告が 1 回出るため
        self._energize_expected_since: float | None = None
        # サーバー起動時刻。`INFO` 未受信の猶予 (`_FIRMWARE_INFO_GRACE_S`) の起点で、
        # `_energize_expected_since` と違って緊急停止のたびには置き直さない ——
        # `INFO` は自作モタドラが励磁状態と無関係に 1Hz で送り続けるので、猶予は
        # 起動 1 回だけで足りる (置き直すと緊急停止のたびに検出が遅れる)
        self._server_started_at: float | None = None
        # 直近の有効化で励磁できなかったモータ (ロボット名 -> モータ名)。
        # 送信失敗もフィードバック待ちの失敗もここへ集約し、`safety` に載せて配信する。
        # **緊急停止で消さない。** 停止中に報告を止めるのは `_unenergized_motors` の
        # 緊急停止ガード 1 箇所の役目で、こちらでも消すと「壊しても落ちない層」が
        # 増えるだけになる (どちらか片方を消しても症状が出ないので、後で誰かが
        # 本物のガードの方を消しても気付けない)
        self._inactive_motors: dict[str, list[str]] = {}
        # 緊急停止解除の再励磁タスク。解除ハンドラはこれを待たずに返る (待つと
        # その操縦者の WS が数秒間 1 通も処理しなくなる)。GC で消えないよう
        # 参照を保持する — 取りこぼすと、再励磁が途中で消えたことに誰も気付けない
        self._reactivate_tasks: set[asyncio.Task[None]] = set()
        # 単発の再励磁コマンド (`reenergize_motors`) の実行中タスク。ロボット名 →
        # タスクで、同じロボットへの二重投入を防ぐ (in-flight のまま次の押下が来ると
        # 同じバスへ `activate_motors` が 2 重に走り、フィードバック待ちが競合する)
        self._reenergize_tasks: dict[str, asyncio.Task[None]] = {}
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

        # アクチュエータ動作確認 (lib/server_motor_check.py)。**両ハンドを 1 本の
        # シーケンスで順に駆動する** (sequences/motor_check.py)。機体ごとに独立した確認だと
        # 2 つを同時に起動でき、可動域の重なる位置で干渉しうる。
        # 環境側の条件 (フェーズ・緊急停止・各ロボットの制御権) だけをここから渡し、
        # 起動・中断・配信はあちらが持つ
        self._motor_check = MotorCheckController(
            environment_deny=self._motor_check_environment_deny,
            pausables=self._motor_check_pausables,
            is_e_stop_active=lambda: self._e_stop_active,
            broadcast=self._ws.broadcast_json,
        )

        self._verify_command_handlers()

    def _verify_command_handlers(self) -> None:
        """語彙が宣言したハンドラが実在することを起動時に 1 度だけ確かめる。

        ディスパッチは ``getattr(self, spec.handler)`` の文字列引きなので、
        メソッド名を変えても静的には何も検出されない。起動もするが、操縦者が
        そのボタンを押した瞬間に ``AttributeError`` になる —— 試合中に初めて
        分かる壊れ方で、しかも拒否通知も出ないので画面から原因が読めない。
        ``CommandSpec`` がゲート方針を必ず宣言させているのと同じで、ハンドラ名の
        実在確認はその宣言の完結にあたる。
        """
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
        """開発用コマンドが解禁されているか。コマンドゲートと server_info が参照する。"""
        return self._dev_tools

    @property
    def _reactivating(self) -> bool:
        """緊急停止解除の再励磁が進行中か。"""
        return any(not task.done() for task in self._reactivate_tasks)

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
        target_refreshers: list[TargetRefresher] | None = None,
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

    def set_motor_check_sequence(self, sequence: Sequence) -> None:
        """統合動作確認シーケンスを登録する。

        どのロボットにも属さない。両ハンドのアクチュエータを 1 つの順序で駆動するため、
        `RobotContext` の下に置くと「どちらの機体のものか」が答えられなくなる。
        """
        self._motor_check.set_sequence(sequence, court=self.match.court)

    def _apply_court(self) -> None:
        self._motor_check.set_court(self.match.court)
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
        # 動作確認エンドポイントも SPA フォールバックより前に登録する。
        # 両ハンド統合の 1 本なので robot を取らない
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
        overalls: list[BusHealth] = []
        for robot_name in self._robots:
            snap = self._compute_health(robot_name)
            robots_payload[robot_name] = snap.to_dict()
            overalls.append(snap.overall)

        # 最悪値への集約は lib.health だけが知っている。ランク表をここへ写すと
        # 「Monitor は READY と言うのに操縦者の画面は異常と言う」状態が作れる
        overall = worst_bus_health(overalls)

        # OK 以外は監視系から異常を検出できるよう 503 を返す
        status = 200 if overall is BusHealth.OK else 503
        return web.json_response(
            {"overall": overall.value, "robots": robots_payload},
            status=status,
        )

    async def _on_startup(self, app: web.Application) -> None:
        # `main()` は CANManager.run() (= 起動時設定と励磁) を終えてから
        # `server.start()` を呼ぶので、この時点以降は全モータが励磁されているのが
        # 正しい状態になる。起動時に励磁できなかったモータも同じ経路で画面に出す
        self._energize_expected_since = time.time()
        self._server_started_at = time.time()
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

        # 再励磁は応答の返らないモータで 1 台 0.5 秒待つ。終了処理がそれを
        # 待つと、CAN を落とす後始末まで到達するのが遅れる
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

        # server_info と match_state と motor_check_state は接続直後にしか送らない。
        # server_info は起動オプション由来で試合中に変わらないため定期配信に載せず、
        # 残り 2 つは変化時のみ配信するのでスナップショットが要る
        # (これがないとリロード直後のクライアントが現在のモード/フェーズを知れず、
        # 動作確認の実行中に繋いだ画面は「未実行」を出したまま止まる)。
        #
        # **この 3 通も `WsHub.send_or_drop` を通す。** 生の `send_str` は相手が読まなく
        # なると無期限に待つので、スリープに入りかけたノート PC が 1 台繋いだだけで
        # この接続ハンドラが返らなくなり、`finally` の切り離しも
        # 走らない (配信ループは `ws.closed` にならない相手へ送り続ける)。
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
        """起動オプション・config 由来の、試合中に変わらない情報。接続直後に 1 度だけ送る。

        開発用ボタンの表示可否をクライアント側のビルド時定数で決めると、同じ
        `web/dist` を配る本番と開発で再ビルドが要る (= 切り替えとして機能しない)。
        正はサーバーが持ち、UI は配られた値を表示に反映するだけにする。

        温度しきい値も同じ性質 (config 由来で試合中には変わらない) なのでここに載せる。
        UI が独自のしきい値を持つと、config を変えても画面の判定だけが古い値のまま残り、
        同じモータについてサーバーと UI が違う答えを出す。載せるのは UI が温度の色分けに
        使う 2 値だけで、使わない値は配らない (配ると「配られているのだから使ってよい」
        という別の写しの根拠になる)。
        """
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
        """1 コマンドを受理して 4 段のゲートに掛け、通ったものだけ実行する。

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

        # ゲートは 5 段。開発用ゲート (この起動にそのコマンドが存在するか) が最初で、
        # 次にフェーズゲート (試合進行として許されるか)、通ったものだけ緊急停止ゲート
        # (今モータを動かしてよいか) に掛ける。フェーズが MATCH のままでも緊急停止中は
        # START を通してはならず、match_start は READY で受理されうるのでフェーズ遷移より
        # 手前で止める。開発用ゲートを先頭に置くのは、無効な起動での拒否理由が
        # 「フェーズが違う」ではなく「この起動には無い機能」であるべきだから。
        # 最後に手動操縦ゲート (対象ロボットが手動モードなら塞ぐ)。手動 → シーケンス
        # 復帰の入口は 2 つあり、`_apply_operation_mode` は手動へ入る側で
        # `_stop_sequence` により制御権を奪うが、**手動に入った後に届く
        # sequence_start / sequence_jump / trigger を弾く経路がここまで無かった**
        # (CommandSpec にモードゲートの概念自体が無く、`_manual_target` の判定は
        # 逆方向 = 手動指令がシーケンスモード中に来た場合しか見ていなかった)。
        # 手動とシーケンスは同じ `AxisHandle.set_target_value` を通るため、
        # 塞がないとジョグ中の軸へシーケンスが別の目標値を書きに来る。
        # 最後が再励磁ゲート (対象ロボットの励磁を今書き換えている最中か)。手動と
        # 同じ衝突がシーケンス側にもある —— 再励磁の `activate_motors` が書く
        # 「フォルト前の現在角」が `move_to` の目標を上書きすると、`wait_reached` は
        # 動かない位置を見続けて `SequenceTimeoutError` で止まる。**塞ぐのは
        # この向きだけ**で、逆 (シーケンス実行中の再励磁) は通す (理由は
        # `CommandSpec.blocked_during_reenergize`)。
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
            # **ここは `_ws_handler` の受信ループから await されている。** 抜けさせると
            # `async for msg in ws` ごと降り、その操縦者は画面から何も送れなくなる
            # (試合中なら E-STOP を押す手段まで失う)。かつては `_run_manual` だけが
            # 自前で握っており、他のハンドラが投げうる経路は
            # 無防備なままだった。握りをディスパッチ 1 箇所に置けば、コマンドを
            # 足す人が同じ握りを書き写す必要が無くなる。
            # 拒否経路はそのコマンドが宣言したものを使う (動作確認だけは専用チャネル)
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
        """拒否を、そのコマンドが宣言した経路で操縦者へ返す。"""
        if spec.reject_channel is RejectChannel.MOTOR_CHECK_ERROR:
            await self._motor_check.report_error(reason)
        else:
            await self._reject_command(requester, spec.name, reason)

    def _manual_mode_deny_reason(self, spec: CommandSpec, data: dict) -> str | None:
        """spec が手動操縦ゲートの対象で、かつ対象ロボットが今手動モードなら理由を返す。

        `CommandSpec.manual_deny_reason()` は「このコマンドをゲート対象にしたか」
        しか知らない (ロボットごとの `OperationMode` は `RobotContext` が持つため)。
        ここで data["robot"] から実際のモードを引いて掛け合わせる。

        ロボット名が無い・未知・見つからない場合は素通しする (deny しない) —
        既存のハンドラ側 (`if robot_name and robot_name in self._robots:`) が
        同じ条件で silent ignore しており、ここで先取りして拒否理由を返すと
        「未知のロボット」という別の失敗が「手動操縦中」の理由で覆い隠される。
        """
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
        """spec が再励磁ゲートの対象で、かつ対象ロボットの再励磁が今 in-flight なら理由を返す。

        `_manual_mode_deny_reason` と同じ形 —— `CommandSpec` は「ゲート対象にしたか」
        しか知らず、ロボットごとの在飛状態はサーバーが `_reenergize_tasks` から引く。
        ロボット名が無い・未知なら素通しするのも同じ理由 (未知のロボットという別の
        失敗を、別の理由文で覆い隠さない)。
        """
        reason = spec.reenergize_deny_reason()
        if reason is None:
            return None
        robot_name = data.get("robot")
        if not isinstance(robot_name, str):
            return None
        if not self._is_reenergizing(robot_name):
            return None
        return reason

    def _is_reenergizing(self, robot_name: str) -> bool:
        """このロボットの単発再励磁が in-flight か。**判定はここ 1 箇所だけが持つ。**

        同じ判定が 4 箇所 (シーケンス系ゲート・手動への切替・手動指令・動作確認の
        起動可否) と配信 (`_safety_state`) から要る。`not task.done()` を書き写すと、
        タスクの持ち方を変えたときに一部だけが古い判定のまま残り、**塞いだつもりの
        経路だけが素通りする**。
        """
        task = self._reenergize_tasks.get(robot_name)
        return task is not None and not task.done()

    # ------------------------------------------------------------------ #
    #  コマンドハンドラ (lib/commands.py の CommandSpec.handler から引かれる)
    #  ゲートは handle_command で済んでいるので、ここでは実行だけを行う。
    # ------------------------------------------------------------------ #

    async def _cmd_trigger(self, data: dict, _requester: WSOrNone) -> None:
        robot_name = data.get("robot")
        if robot_name and robot_name in self._robots:
            self._robots[robot_name].sequence.trigger()
            logger.info("trigger: %s", robot_name)

    async def _cmd_e_stop(self, _data: dict, _requester: WSOrNone) -> None:
        await self.activate_e_stop()

    async def _cmd_e_stop_release(self, _data: dict, requester: WSOrNone) -> None:
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
        # **再励磁を待たない。** `async for msg in ws` は 1 接続あたり完全に直列なので、
        # ここで待つとそのあいだ次の 1 通が処理されない。フィードバックの返らない
        # モータは 1 台 0.5 秒待つため、CAN が落ちている状況 —— まさに緊急停止を
        # 押した状況 —— では数秒に達し、**E-STOP の押し直しすら効かなくなる**。
        # 進捗は `safety.unenergized_motors` として配信され続ける。
        #
        # **前回の解除の再励磁を畳むのもここではない** (`_reactivate_motors` の冒頭
        # が持つ)。畳み込みは最悪 `_REENERGIZE_CANCEL_TIMEOUT_S` 待つので、ここへ
        # 置くとすぐ上の理由がそのまま当てはまり、自分でこの性質を破ることになる。
        # 単発再励磁の畳み込み (`_settle_pending_reenergize`) がハンドラではなく
        # `_reactivate_motors` の中から呼ばれているのと同じ形に揃えてある
        task = asyncio.create_task(self._reactivate_motors())
        # GC で消えないよう参照を保持する (WsHub の切り離しタスクと同じ形)
        self._reactivate_tasks.add(task)
        task.add_done_callback(self._reactivate_tasks.discard)

    async def _cmd_reenergize_motors(self, data: dict, requester: WSOrNone) -> None:
        """励磁が落ちたモータを、機体を止めずに戻す明示操作 (docs/checks_and_health.md)。

        ロボット名が無い・未知の場合は素通しする (拒否理由を返さない) —
        `_manual_mode_deny_reason` と同じ理由で、未知のロボットという別の失敗を
        別の理由文で覆い隠さないため。
        """
        robot_name = data.get("robot")
        if not isinstance(robot_name, str) or robot_name not in self._robots:
            return

        if self._motor_check.running:
            # 動作確認は両ハンドの駆動を 1 本のシーケンスで占有する。ここで励磁を
            # 差し込むと、確認中の駆動と重なって「誰が何を動かしているか」が読めなくなる
            await self._reject_command(
                requester, "reenergize_motors", "動作確認の実行中は再励磁できません"
            )
            return
        if self._reactivating:
            # 緊急停止解除の再励磁は全ロボットぶんまとめて走る。同じバスへ
            # activate_motors が二重に走ると、フィードバック待ちが競合する
            await self._reject_command(
                requester, "reenergize_motors", "緊急停止解除の再励磁が進行中です"
            )
            return
        if self._is_reenergizing(robot_name):
            await self._reject_command(requester, "reenergize_motors", "再励磁の処理中です")
            return

        logger.info("再励磁コマンド受信: robot=%s", robot_name)
        # **再励磁を待たない。** 理由は e_stop_release と同じ (応答の無いモータで
        # 1 台 0.5 秒待つため、待つとその操縦者の WS が数秒間 1 通も処理しなくなる)
        task = asyncio.create_task(self._reenergize_motors(robot_name))
        self._reenergize_tasks[robot_name] = task
        # 完了とこのコールバックの実行のあいだには `call_soon` 1 回ぶんの窓がある。
        # そこへ次の押下が入ると (在飛ガードは `not task.done()` なので通る) 同じ
        # キーへ新しいタスクが載るため、**無条件に pop すると新しいタスクの登録ごと
        # 消える** —— `_is_reenergizing` が False を返し、二重投入・シーケンス系
        # ゲート・動作確認の排他がまとめて外れる。識別子を見て自分自身のときだけ
        # 取り除く。なお辞書はロボット名で上書きされるので、この掃除が無くても
        # 溜まるのは 1 ロボット 1 エントリだけである (窓が狭くテストは持っていない)
        task.add_done_callback(
            lambda t, name=robot_name: (
                self._reenergize_tasks.pop(name, None)
                if self._reenergize_tasks.get(name) is t
                else None
            )
        )

    async def _cmd_health_check(self, _data: dict, _requester: WSOrNone) -> None:
        # クライアントからの即時ヘルス要求。次回ループを待たずに即配信する。
        await self._broadcast_state()

    async def _cmd_sequence_jump(self, data: dict, _requester: WSOrNone) -> None:
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

    async def _cmd_sequence_stop(self, data: dict, _requester: WSOrNone) -> None:
        robot_name = data.get("robot")
        if robot_name and robot_name in self._robots:
            # 停止経路は 1 本に寄せる。`request_stop` を直に呼ぶと未処理の開始要求が
            # 残り、STOP の直後にそれが発火して先頭から全工程を走り切る
            self._stop_sequence(self._robots[robot_name])
            logger.info("sequence_stop: %s", robot_name)

    async def _cmd_sequence_start(self, data: dict, _requester: WSOrNone) -> None:
        robot_name = data.get("robot")
        if robot_name and robot_name in self._robots:
            self._robots[robot_name].sequence.request_start()
            logger.info("sequence_start: %s", robot_name)

    async def _cmd_motor_check_start(self, _data: dict, _requester: WSOrNone) -> None:
        # 両ハンド統合の 1 本なので robot を取らない
        await self._motor_check.start()

    async def _cmd_motor_check_abort(self, _data: dict, _requester: WSOrNone) -> None:
        self._motor_check.abort()

    # ------------------------------------------------------------------ #
    #  手動操縦
    # ------------------------------------------------------------------ #

    async def _cmd_set_operation_mode(self, data: dict, requester: WSOrNone) -> None:
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
            # 二重起動の判定と同じく実行タスクの生死で見る。シーケンスの is_running は
            # タスク生成から run() 開始までのあいだ False で、そこを素通しすると
            # 駆動中の動作確認と手動指令が同じモータを奪い合う。
            # **動作確認は両ハンドを 1 本で駆動する**ので、どちらのロボットを手動へ
            # 移そうとしても拒否する (片方だけ許すと確認の途中で干渉する)
            if self._motor_check.running:
                await self._reject_command(
                    requester,
                    "set_operation_mode",
                    "動作確認の実行中は手動操縦へ切り替えられません",
                )
                return False
            # このロボットの再励磁が in-flight なら拒否する。手動へ入った直後に
            # ジョグを送ると、再励磁の `activate_motors` が書く「フォルト前の
            # 現在角」で上書きされうる (同じモータへの目標書き込みが競合する)
            if self._is_reenergizing(robot_name):
                await self._reject_command(
                    requester, "set_operation_mode", f"'{robot_name}' の再励磁が処理中です"
                )
                return False
            # 制御権を必ず手放させる。破棄しないと、切替直前に届いた開始要求が
            # 手動で機構を動かしている最中に発火する
            self._stop_sequence(ctx)

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

        **このロボットの再励磁が in-flight なら拒否する。** `reenergize_motors` は
        手動操縦中も意図的に塞がない (`lib/commands.py`) ので、手動へ「入る」ときの
        ガード (`_apply_operation_mode`) だけでは、既に手動中のロボットへ再励磁を
        かけた最中に届くジョグを塞げない。ジョグは再励磁の `activate_motors` が
        書く「フォルト前の現在角」目標と同じモータへ競合しうる (敵対的レビュー指摘。
        3 コマンド共通のこの関門に置くのは、ハンドラごとに書くと足し忘れる経路が
        できるのを避けるため)。
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
        if self._is_reenergizing(robot_name):
            await self._reject_command(requester, command, f"'{robot_name}' の再励磁が処理中です")
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

        握るのは ``ManualControlError`` だけ。これは「可動範囲外」「連続操作の
        対象外」のように**操縦者がそのまま読める理由**を持つ拒否で、想定内の応答に
        あたる。位置名の誤りや送信失敗のような想定外の例外は ``handle_command`` の
        ガードが受け止める —— 同じ握りをここにも置くと、握りが 2 箇所に増えた
        ぶんだけ「片方だけ直された」状態が作れる。
        """
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
        # 指差喚呼を飛ばしたことは必ずログに残す。試合直前のログを追ったときに
        # 「点検を実施したのか、開発用ボタンで埋めたのか」が区別できないと困る
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
            # 実周期の集計をここで 1 行残してから 0 に落とす。リセット点を
            # match_start だけにすると集計窓が [試合N開始, 試合N+1開始) になり、
            # 試合後の finished・match_reset・次のセッティングタイム・両ハンドを
            # 一巡する動作確認 (まさに乱れの発生源) が丸ごと混ざって、1 行が
            # 「試合 N の集計」を名乗れなくなる。しかもその日の最後の試合は
            # 次の match_start が来ないので永久に journal へ出ない —— 一番読みたい
            # 1 試合が抜ける。試合の終わりを決めるのは操縦者の match_finish なので、
            # 「試合 N ぶん」の境界もここにしかない。
            for ctx in self._robots.values():
                for task in self._periodic_tasks(ctx):
                    task.log_jitter_summary()
                    task.reset_jitter_stats()
            await self._broadcast_match_state()

    async def _cmd_match_reset(self, _data: dict, requester: WSOrNone) -> None:
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

    def _motor_command_state(self, robot_name: str, motor_name: str) -> dict[str, object]:
        """PC が基板へ最後に送った指令値と、その種別。一度も送っていなければ None。

        **フィードバックを持たないモータ (DC 基板・電磁弁基板) に対する唯一の
        「今どうなっているか」である。** どちらも測る手段を持たないので配信の 4 値は
        すべて `None` になり、指令まで出さないと画面はそのモータについて何も言えない。

        **M3508 位置制御ループが持つ *軌道の中間目標* と混ぜてはならない。**
        あちらは速度・加速度で制限しながら毎周期動く値で、こちらは *PC が基板へ
        最後に送った値そのもの* である (`GenericTargetRefresher` が 20Hz で
        再送し続けているのはこの値)。1 つに畳むと、「今どこを狙っているか」と
        「何を指令したか」が同じ欄の中で入れ替わる。

        出どころは ``MotorHandle`` ただ 1 つで、手動・シーケンス・動作確認の
        どの経路から出した指令も同じハンドルを通る (`main._wire_one_robot` が
        `MotorGroup` を 1 つだけ作って共有している)。緊急停止では
        `GenericTargetRefresher.clear_targets()` がハンドルの目標ごと捨てるので、
        **停止中の DC 基板・電磁弁基板は `None` に戻る** —— 停止しているのに
        `→0.30` と出ていたら、操縦者は「まだ出し続けている」と読む。

        位置定数を読めていないロボット (`has_motors` が False) と、
        `MotorGroup` に居ないモータは `None` へ倒す (ヘルスや配信を落とさない)。
        """
        sequence = self._robots[robot_name].sequence
        if not sequence.has_motors:
            return {"command": None, "command_mode": None}

        group = sequence.motors
        if motor_name not in group:
            return {"command": None, "command_mode": None}

        handle = group[motor_name]
        mode = handle.mode
        return {"command": handle.target, "command_mode": None if mode is None else mode.value}

    # ------------------------------------------------------------------ #
    #  試合状態 (コート / フェーズ / チェックリスト)
    # ------------------------------------------------------------------ #

    async def _handle_set_court(
        self,
        data: dict,
        requester: WSOrNone = None,
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

    @staticmethod
    def _periodic_tasks(ctx: RobotContext) -> tuple[PeriodicTask, ...]:
        """1 ロボットぶんの周期タスク全部 (実周期の集計とリセットはこの単位で回す)。

        3 種を並べる箇所が match_start / match_finish の 2 つあるので、1 つに
        まとめておく —— 別々に書くと、4 種目の周期タスクが増えたときに片方だけが
        古いまま残り、症状は「その試合の集計にだけ 1 本足りない」になる。
        """
        return (*ctx.position_loops, *ctx.sync_monitors, *ctx.target_refreshers)

    async def _handle_match_start(self, requester: WSOrNone = None) -> None:
        # **動作確認の実行中は試合に入れない。** フェーズが MATCH になると
        # sequence_start が解禁され、両ハンドを一巡している統合動作確認と通常
        # シーケンスが同じアクチュエータへ同時に指令を出す。ここで `abort()` へ
        # 倒さないのは、操縦者が意図していない中断より拒否のほうが安全だから
        # (止めたければ motor_check_abort が別にある)
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

        # 開始直前にもう一度流し込む (取りこぼすとシーケンスが逆コートの分岐で動く)
        self._apply_court()

        # 試合単位でリセットする 2 つ。ここは**前縁リセット** —— 準備中 (配線確認・
        # 動作確認) に踏んだぶんを洗い流し、試合中の数字を「この試合で起きたこと」
        # だけにする。match_reset ではなく match_start なのは、finished (結果確認中)
        # に直前の試合の記録を消さないため。CANManager も PeriodicTask も「試合」を
        # 知らないぶん、いつ呼ぶかはここ (サーバー) が決める。
        # ジッタの集計 1 行は match_finish が出すので、ここでは黙って 0 に戻すだけ。
        for ctx in self._robots.values():
            ctx.can_manager.reset_rx_down_episodes()
            for task in self._periodic_tasks(ctx):
                task.reset_jitter_stats()

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
        # 動作確認はタスク生成から run() 開始までのあいだ is_running=False の窓を
        # 持つ。そこを条件にすると起動しかけの動作確認だけが停止をすり抜けるため、
        # 状態を見ずに中断を要求する (要求は `_stop_event` に残り、run() が捨てない)
        self._motor_check.abort()
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
            self._stop_all_sequences()
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

    async def _send_e_stop_clear_broadcast(self) -> None:
        """全バスへブロードキャストの緊急停止解除フレームを送る。

        **停止と解除は対称でなければならない。** 停止は `_send_e_stop_frames` が
        `0x0FF` をバスへ流すのでバス上の全基板・全チャンネルがラッチするのに対し、
        解除は `activation_steps()` が device_id 宛に個別送信するため
        **yaml に登録されたモータにしか届かない**。PC の管轄外のチャンネル
        (ベンチ設定で一部だけ動かす / 増設した基板が yaml に無い / 片方のロボット
        だけ起動する) は永久にラッチされたまま残り、基板の LED は 1 チャンネルでも
        ラッチがあれば橙になるので **全基板が橙のまま戻らない**。操縦者からは
        機体が復帰不能に見える (実機で発生)。

        **ブロードキャストしても機体は動かない。** 停止時に目標値が捨てられており
        (DC は duty 0 / サーボは現在角保持 / 電磁弁は OFF)、ファーム側の
        `MotorSafety::isOutputAllowed()` は `SET_TARGET` を 1 通も受けるまで出力を
        許可しない (仕様書 §5.4)。物理停止スイッチが押されている間はファームが
        毎ループ再ラッチするので「押している間は絶対に動かない」も保たれる。

        1 バスの送信失敗で他のバスを諦めないのは停止側と同じ。
        """
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
            "unenergized_motors": self._unenergized_motors(robot_name),
            "firmware_unconfirmed_motors": self._firmware_unconfirmed_motors(robot_name),
            # 単発の再励磁が処理中か。**押した後の 0.1〜1.5 秒は
            # `unenergized_motors` が消えない**ので、これが無いと操縦者には
            # 「押しても何も起きない」ようにしか見えず 2 回目を押す (そして
            # 「再励磁の処理中です」というトーストを受け取る)。可否も理由も
            # サーバーが持つ、という原則どおり在飛そのものを配る
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
        """励磁されているべきなのに無励磁のモータ。

        **これは「画面が正常に見えるのに機体が動かない」型の異常である。**
        DM3520 はドライバの通信途絶保護や電源の瞬断で励磁が外れるが、その後も
        フィードバックは正常に届き、`is_fault()` にも掛からないのでモータのヘルスは
        OK のまま。PC は 20Hz で位置指令を送り続け、CAN のカウンタにも異常は出ない。
        操縦者から見えるのは「指令しても動かない」だけで、原因を示す表示がどこにも無い。

        励磁状態を報告しないドライバ (`is_energized()` が None) は対象外。
        「分からない」を「無励磁」へ倒すと、自作モタドラと C620 が常時警告を出す。

        緊急停止中は無励磁が正しいので何も返さない。解除・起動の直後も、enable が
        次のフィードバックへ反映されるまでの 1 周期ぶんは猶予する。
        """
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
        # 有効化そのものに失敗したモータは、フィードバックが届いていなくても出す
        # (`is_energized()` は最後に届いた値しか見ないので、応答の無いモータは
        # 「無励磁と分かっている」側に入らない)
        names.update(self._inactive_motors.get(robot_name, ()))
        return sorted(names)

    def _firmware_unconfirmed_motors(self, robot_name: str) -> list[str]:
        """起動の猶予を過ぎても自己申告 (`INFO`) を一度も受けていない自作モタドラ。

        **これは「異常」ではなく「焼き忘れ検出が働いていない」ことの報告である。**
        `INFO` の未受信は FAULT にしない (送信バッファの都合でも起きるため。
        CLAUDE.md 「送信バッファの本数は 3 枚で違う」節) が、その間は
        `GenericDriver.info_mismatch` による焼き忘れ検出も一緒に働かなくなる。
        黙って無効になると誰も気付けないので、ここで別の状態として拾う。

        `INFO` を送らないドライバ (`firmware_confirmed()` が None) は対象外 ——
        M3508 / EDULITE 05 / DM3520 を混ぜると全モータが常時この状態になる。

        **フィードバックが途絶えている (STALE) モータも対象外。** 基板が丸ごと
        落ちていれば `INFO` も当然来ないが、それは `CANManager.health()` が全
        チャンネルを STALE に倒して `evaluateHealth` が warning として大声で言い、
        診断ツリーを強制展開する経路が既にある。ここでも言うと同じ事実を 2 度
        描くことになり、しかも**この報告の手当ては STALE とは別物になる** ——
        残したいのは「`FEEDBACK` は 10ms で届き続けているのに `INFO` だけが
        1 通も出ない」という、CLAUDE.md 「送信バッファの本数は 3 枚で違う」節が
        書く壊れ方だけである。そこで電源・CAN 配線を疑っても必ず何も見つからない
        (配線が正常だから `FEEDBACK` が来ている)。鮮度のしきい値は
        `HealthThresholds` から来た 1 つだけを使い、ここに別名の値を置かない。

        **dry-run は対象外。** virtual バスは `INFO` を 1 通も返さないので、猶予を
        過ぎれば全自作モタドラが恒久的に「未確認」になり、机上で画面を確かめられなく
        なる (`server_dryrun.py` が見栄えの値だけを作る領域と同じ理由)。

        **見ているのは `motors` だけ。** ファームはセンサスロットも `INFO` を送る
        (仕様書 §5.2) が、現状 `main.py` はセンサを `expected_firmware` なしに
        生成するので照合対象そのものが無い。`sensors:` に `expected_firmware` を
        書けるようにする日には、ここも `ctx.can_manager.sensors` を見ること。
        """
        if self._dry_run:
            return []

        since = self._server_started_at
        if since is None or time.time() - since < _FIRMWARE_INFO_GRACE_S:
            return []

        ctx = self._robots[robot_name]
        freshness = FeedbackFreshness(
            ctx.can_manager.last_feedback_at, timeout_ms=self._health.feedback_timeout_ms
        )
        # 1 周期に 1 回だけ取る (モータごとに取り直すと同じ配信の中で基準時刻がずれる)
        now = freshness.now()
        return sorted(
            motor_name
            for motor_name, motor in ctx.can_manager.motors.items()
            if motor.firmware_confirmed() is False and not freshness.is_stale(motor_name, now)
        )

    async def _reactivate_motors(self) -> None:
        """緊急停止解除後にモータの励磁を戻す。

        EDULITE 05 は非常停止で無励磁になるため、解除で再励磁しないと以後の位置指令が
        一切効かない。再励磁自体はドライバ側が現在角を保持目標に書いてから行うので、
        解除操作そのものでロボットが動くことはない。

        **別タスクで走る。** 解除ハンドラはこの完了を待たない (待つと、その操縦者の
        WS が数秒間 1 通も処理しなくなる)。そのため、解除フレームを送り終えた
        時刻を記録するのもここの責務になる —— 送信より前にその時刻を置くと、
        まだ解除フレームが届いていない基板のフィードバックを「解除後の報告」として
        信じてしまい、解除した瞬間にサーバーが自分で止め直す。

        **自作モタドラのラッチ解除は励磁より先に、全ロボットぶんまとめて行う。**
        励磁の中断 (`should_abort`) はロボットを順に処理するので、1 台目の最中に
        緊急停止が再び入ると 2 台目へは解除フレームが 1 通も飛ばない。ラッチの
        外れない基板は緊急停止ビットを報告し続け、それを `_detect_board_e_stop` が
        拾って停止を再発動するため、**解除操作のたびに同じロボットだけが
        取り残されて永久に復帰できなくなる** (実機で発生)。ラッチ解除は
        それ自体では機体を動かさないので中断する理由が無い (根拠は
        `CANManager.clear_e_stop_latches`)。

        解除は 3 段になる: ①全バスへブロードキャスト解除 (バス上の全基板。PC の
        管轄外のチャンネルを救う唯一の経路) → ②管轄内モータへ個別のラッチ解除
        (中断しない) → ③励磁 (中断あり)。①と②が重なるのは意図的で、①は
        「バス上の全基板へ届く」ことを、②は「PC が把握しているモータへ確実に
        届く」ことをそれぞれ担う。

        **③の直前に、同じロボットの `_reenergize_motors` が in-flight なら畳む**
        (`_settle_pending_reenergize`)。両者が同じロボットの `activate_motors` を
        並走させると、片方の `_wait_fresh_feedback` が送るプローブ
        (EDULITE 05 / DM3520 とも `feedback_probe_message()` = disable) が、
        もう片方が enable したばかりのモータへ届く —— DM3520 は disable で
        自重落下するので、「戻した直後にもう一度落とす」形で
        `_reenergize_motors` 自身の存在意義を壊す。
        **①より前に、前回の解除の再励磁が残っていれば畳む**
        (`_settle_pending_reactivation`)。理由は上と同型 —— 2 本の
        `_reactivate_motors` が並走しても、片方のプローブがもう片方の enable 直後の
        モータへ届く。**畳み込みを 2 つともここに置くのは意図的で**、
        `_cmd_e_stop_release` へ移すと畳み込みが最悪
        `_REENERGIZE_CANCEL_TIMEOUT_S` 待つぶんだけ解除の受理が止まり、
        「解除は再励磁を待たない」性質を自分で破ることになる。

        **解除コマンドの受理そのものは拒否・待機させない** (拒否すると「解除の
        たびに同じロボットが取り残される」実機事故と同型になる)。ここ
        (バックグラウンドの再励磁タスク) だけが古いタスクを畳んでから
        自分の励磁へ進む。
        """
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

        # 解除して有効化を試みた以上、以降は励磁されているのが正しい状態になる。
        # 起点を置くのはここだけで、猶予の判定は `_unenergized_motors` が行う
        self._energize_expected_since = time.time()

        # 解除フレームはこの時点で送り終えている。以降に届いたフィードバックで
        # まだ緊急停止ビットが立っていれば、それは基板側にまだ止まる理由がある
        # (物理停止スイッチが押されたまま等) ということなので、改めて停止させる
        self._board_e_stop_ignore_before = time.time()

        # 有効化の途中で再び緊急停止が入ると、中断判定をすり抜けた enable が
        # 停止フレームより後に届きうる。念のため停止フレームを送り直す。
        if self._e_stop_active:
            logger.warning("有効化中に緊急停止が再度入ったため停止フレームを再送します")
            try:
                await self._send_e_stop_frames()
            except Exception:
                logger.exception("E-STOP 停止フレーム再送に失敗")

    async def _settle_pending_reenergize(self, robot_name: str) -> None:
        """緊急停止解除の励磁へ進む前に、同じロボットの再励磁タスクを畳む。

        **「待つ」ではなく「キャンセルしてから有界に待つ」。** かつては素の
        `await pending` で、根拠を「古いタスク自身が有界だから」に置いていた。
        **有界なのは `_wait_fresh_feedback` の deadline だけである** ——
        `CANManager.send_to_bus` は `_run_blocking(bus.send, msg)` をタイムアウト
        無しで待つので、SocketCAN の送信キューが詰まっていれば `bus.send` は
        ブロックしうる。そしてそれは `cbc-can-watchdog` が bus-off を疑っている
        状況、つまり **まさに緊急停止を押した状況**である。素の await のままだと
        「緊急停止解除の再励磁が無期限に進まない」経路が理屈上残る。

        **キャンセルだけでは足りない。** エグゼキュータのスレッドへ入った
        `bus.send` は `Task.cancel()` では止まらず、完了するまで
        `CancelledError` が投げ込まれない。上限を必ず添える。

        **待ちきれなくても先へ進む。** この直後に `only=None` で全モータを
        励磁し直すので、キャンセルで中途半端に残った状態はそこで上書きされる。
        並走を完全には防げないが、防げないのは「PC が CAN を送れなくなっている」
        場面に限られ、そこで解除が永久に進まないほうが重い。黙って進まないよう
        ログには必ず残す。
        """
        pending = self._reenergize_tasks.get(robot_name)
        if pending is None or pending.done():
            return
        pending.cancel()
        # `asyncio.wait` は中の例外 (CancelledError を含む) を送出しない。
        # `await pending` や `wait_for` だとキャンセル済みタスクの CancelledError が
        # そのまま伝播し、**この再励磁タスク自身がキャンセルされたことになる**
        done, _still_running = await asyncio.wait({pending}, timeout=_REENERGIZE_CANCEL_TIMEOUT_S)
        if not done:
            logger.error(
                "再励磁タスクが %.1fs 以内に畳めませんでした: robot=%s"
                " (CAN の送信が詰まっている可能性があります)。解除の励磁を先へ進めます",
                _REENERGIZE_CANCEL_TIMEOUT_S,
                robot_name,
            )

    async def _settle_pending_reactivation(self) -> None:
        """新しい解除の再励磁を始める前に、前回のぶんを畳む。

        畳み方 (キャンセル → 有界待ち → 待ちきれなくても進む) も、呼び出し位置
        (ハンドラではなく `_reactivate_motors` の冒頭) も、その理由も
        `_settle_pending_reenergize` とまったく同じ。違うのは対象だけで、あちらは
        単発の `reenergize_motors` (ロボット単位)、こちらは緊急停止解除の
        `_reactivate_motors` (全ロボットまとめて) を見る。**自分自身
        (`asyncio.current_task()`) は畳む対象から外す** —— `_reactivate_tasks` には
        呼び出し元のタスクも既に載っているので、外さないと自分をキャンセルして
        1 通も送らずに降りる。

        **踏むのは「解除を 2 連打したとき」ではない。** `_cmd_e_stop_release` は
        `not self._e_stop_active` を拒否するので、素の 2 連打では 2 本目が立たない。
        実際に踏むのは **E-STOP → RESET →(同期ずれ検出や操縦者の再押下で)
        E-STOP → RESET** で、1 本目がまだ `activate_motors` の中にいるあいだに
        2 本目が立つ。窓が広がるのは応答の無いモータを 1 台 0.5 秒待っている間、
        つまり CAN が不調なときほど広い。

        **2 系統あることそのものは実装の都合だが、畳み忘れると症状が出る。**
        並走した 2 本のうち片方のプローブ (disable) がもう片方の enable 直後の
        モータへ届くと `sub_lift` が自重で落ち、`_inactive_motors` の書き戻し順に
        よっては画面だけ「無励磁」が残る。

        **`Task.cancel()` は「ラッチ解除は中断しない」原則 (CLAUDE.md) の例外では
        ない。** あの原則が禁じているのは、①ブロードキャスト解除 / ②個別ラッチ解除の
        途中で中断して**取り残されたロボットを残す**ことである。ここでの中断は必ず
        新しい `_reactivate_motors` が①から先頭でやり直す前提とセットなので、
        取り残しは生じない —— 逆に、畳まずに並走させるほうが②の後の③で
        「enable した直後のモータへ disable が届く」形の実害を出す。
        **この前提に依存しているので、畳んだあとに①②を飛ばす経路を作ってはならない。**
        """
        current = asyncio.current_task()
        pending = {
            task for task in self._reactivate_tasks if not task.done() and task is not current
        }
        if not pending:
            return
        for task in pending:
            task.cancel()
        done, _still_running = await asyncio.wait(pending, timeout=_REENERGIZE_CANCEL_TIMEOUT_S)
        if len(done) != len(pending):
            logger.error(
                "解除の再励磁タスクが %.1fs 以内に畳めませんでした"
                " (CAN の送信が詰まっている可能性があります)。新しい解除を先へ進めます",
                _REENERGIZE_CANCEL_TIMEOUT_S,
            )

    async def _activate_motors_for_robot(
        self, robot_name: str, ctx: RobotContext, *, only: Collection[str] | None = None
    ) -> list[str]:
        """1 ロボットぶんの励磁を実行し、無励磁のまま残ったモータ名を記録して返す。

        緊急停止解除の再励磁 (`_reactivate_motors`) と単発の再励磁コマンド
        (`_reenergize_motors`) の共通処理。1 台の送信失敗で残りを諦めない性質は
        `CANManager.activate_motors` 自身が持つので、ここでは例外の握り潰しと
        `_inactive_motors` への記録だけを担う。

        ``only`` は `_reenergize_motors` が無励磁のモータだけに絞るための引数。
        **呼び出し側は ``only`` が前回の `_inactive_motors[robot_name]` を包含すること
        を保証しなければならない** (`_reenergize_motors` の `dropped` は
        `is_energized() is False` に加えて前回の無効化リストそのものを合併して作る)。
        この前提のもとでは「今回の対象全員ぶんの結果」として単純に置き換えればよく、
        対象外のモータの前回の結果を保つマージは要らない。保証しない呼び出しを
        新たに足す場合はここへマージのロジックを戻すこと。
        """
        try:
            inactive = await ctx.can_manager.activate_motors(
                should_abort=lambda: self._e_stop_active, only=only
            )
        except Exception:
            logger.exception("モータ有効化に失敗: robot=%s", robot_name)
            # 例外で丸ごと落ちた場合は対象モータが 1 台も励磁できていない
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
        """1 ロボットぶんの再励磁コマンドの実体。**別タスクで走る** (WS ハンドラは
        `_cmd_reenergize_motors` から投げっぱなしにする — 応答の無いモータで
        1 台 0.5 秒待つため、直列の `async for msg in ws` 上で await すると
        その操縦者の WS が数秒間 1 通も処理しなくなる。理由は `_reactivate_motors`
        と同じ)。

        **無励磁のモータだけ、先に目標をラッチごと剥がしてから励磁する。**
        フォルト直前の目標 (`move_to` の行き先) や `QueryDrivenTargetRefresher` の
        ラッチ済みアイドル目標 (「今の姿勢を保て」) はフォルトで機構が動いたあとも
        古い値のまま残る。剥がさずに `activate_motors()` で現在角を書いて enable
        しても、直後の再送 (最大 50ms 後) がその古い値で上書きして enable の瞬間に
        機構がそこへ動き出す —— 「現在角を目標に書いてから励磁する」保証が
        1 周期で意味を失う。緊急停止解除がこの問題を持たないのは、停止中ずっと
        `is_estop_active()` が True で毎周期現在角を測り直しており、古い値が
        一度も残らないため (`lib/control/target_refresh.py`)。
        剥がす対象を「無励磁のモータだけ」に絞るのは、同じバスの他モータが
        移動中なら `wait_reached` を巻き込んで中断させてしまうため
        (`_TargetRefresherBase.clear_target` 参照)。

        **励磁も無励磁のモータだけに絞る (`activate_motors(only=...)`)。** 絞らずに
        全モータを渡すと、EDULITE 05 / DM3520 の `activate_motor` は健全で移動中の
        モータにも「現在角を書いてから enable」を打ってしまい、動いている軸を
        一瞬止めて enable し直す形で割り込む (`QueryDrivenTargetRefresher` の
        次の再送で実目標へ戻るが、その 1 周期のジャークは避けられる理由が無い)。
        対象は「今無励磁」に加えて「前回の再励磁でも有効化できなかった」モータも含める
        —— `safety.unenergized_motors` が操縦者に見せている集合と同じにして、
        起動直後にフィードバックが来ずに有効化へ進めなかったモータも次の押下で
        リトライできるようにするため。

        **直結ペア (`rotate` = EDULITE x2) の片側だけが無励磁になった場合、
        相方も対象へ含める。** ペアの片側だけを剥がして励磁すると、相方が移動中
        なら「無励磁で連れ回されていた片側」を「相方に逆らって現在角を保持する
        片側」へ変えるだけになり、直後に `SyncMonitor` の偏差超過で試合が止まる
        —— 対称に保つ (CLAUDE.md「ペア軸に片側だけ効く操作を作らない」)。
        `y_axis` (M3508) は `is_energized()` が常に None なのでここには現れず、
        実質 `rotate` だけが対象になる。相方の `wait_reached` が割り込まれるのは
        許容する —— そのペアは片側の無励磁で既に破綻していたので、割り込みは
        「壊れていた」ことの正しい反映であって新たな害ではない。

        **ペアを含む再励磁のあいだも `SyncMonitor` は止めない
        (`suspend_group` を呼ばない)。** 同型の disable → 再 enable を伴う
        零点確定 (`capture_origin_via_set_zero` / `main._suspend_sync_monitoring`)
        は止めるので、揃えたくなるが揃えてはならない。止めない理由は 2 つある:

        - **あちらが止めてよい根拠がここには無い。** 零点確定の根拠は「その間
          モータは無励磁なので、この保護が防ぐ押し合いは原理的に起きない」
          (`SyncMonitor.suspend_group`) だが、再励磁でそれが当てはまるのは
          落ちた側だけで、**相方は励磁されたまま押しうる**。つまりここで
          止めると、この保護が本当に要る瞬間に限って目を塞ぐことになる
        - **もう 1 つの根拠 (座標系が 2 台で違うので偏差という量が定義を失う)
          も当てはまらない。** 原点は動かさないので、この間に出る偏差は
          そのまま機構の実際のずれである

        「遊びのある機構で発報して全体緊急停止 = この機能が避けたかった結果を
        自分で作る」という懸念は理解できるが、**再励磁はずれを増やさない** ——
        剥がした目標も `activate_motors` が書く目標もどちらも「今いる位置」で、
        新しい動きを作らない。既に許容差を超えているなら押す前から超えており、
        押さなくても発報する。そこで止めれば、直したはずのずれが見えないまま
        試合へ戻ることになる。

        **`only` の計算から `activate_motors` 呼び出しまでを try/except で囲う。**
        このタスクは fire-and-forget (`_cmd_reenergize_motors` が await しない) で、
        `add_done_callback` も辞書からの取り除きだけしか見ない。無防備なままだと
        ここでの例外は「`_tasks` を誰も await しないので例外は消える」
        (CLAUDE.md) と同型で握り潰される —— 現状は辞書操作しか無く踏む筋は
        無いが、将来ここへ処理を足したときに同じ穴を空けないための予防線。
        """
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
                # 対象が 1 台も無ければ CAN へ 1 通も出さない (画面が既に閉じた
                # ボタンを遅延で押した等)。`dropped` は `_inactive_motors` を
                # 合併して作るので、空なら前回の失敗も残っていない ——
                # `_inactive_motors` を空で上書きし直す必要も無い
                logger.info("再励磁の対象モータがありません: robot=%s", robot_name)
                return

            for refresher in ctx.target_refreshers:
                for name in dropped.intersection(refresher.motor_names):
                    refresher.clear_target(name)

            # ジョグの起点を捨てる。無励磁のあいだ機構が自重で下がっていた場合、
            # 再励磁後 1 回目のジョグが古い起点から飛ぶ (activate_e_stop の
            # on_e_stop() と同じ理由)。**捨てるのは対象モータが属する軸だけ。**
            # ロボット全体へ効かせると、落ちたのが sub_lift 1 台でも無関係な
            # sub_arm_joint の起点まで消える —— 緊急停止 (機体が止まっている) と
            # 違い、再励磁は「機体を止めずに」が売りなので前提が違う。
            if ctx.manual is not None:
                ctx.manual.reset_axes_for_motors(dropped)

            await self._activate_motors_for_robot(robot_name, ctx, only=dropped)
            # 猶予の起点を置き直す (`_reactivate_motors` と同じ扱い)。enable が
            # 次のフィードバックへ反映されるまでの 1 周期 (実測 ~50ms) は
            # `is_energized()` が古い値のままなので、置き直さないと成功直後の
            # `safety.unenergized_motors` に対象が残り、画面が一瞬「直っていない」
            # と言う。失敗したモータは `_inactive_motors` に残るので、猶予が
            # 明けたところで改めて出る
            self._energize_expected_since = time.time()
        except Exception:
            logger.exception("再励磁処理で予期しない例外: robot=%s", robot_name)
            return

        # 有効化の途中で緊急停止が入ると、中断判定をすり抜けた enable が
        # 停止フレームより後に届きうる。念のため停止フレームを送り直す
        # (_reactivate_motors と同じ理由)
        if self._e_stop_active:
            logger.warning("再励磁中に緊急停止が入ったため停止フレームを再送します")
            try:
                await self._send_e_stop_frames()
            except Exception:
                logger.exception("E-STOP 停止フレーム再送に失敗")

    def _stop_all_sequences(self) -> None:
        """全ロボットのシーケンスを通常停止する (緊急停止と異なり CAN 層は触らない)。"""
        for ctx in self._robots.values():
            self._stop_sequence(ctx)

    def _stop_sequence(self, ctx: RobotContext) -> None:
        """1 台のシーケンスを通常停止する。**破棄が先、停止が後。**

        逆順にすると、停止処理のあいだに届いた開始要求が破棄をすり抜けて残る。
        順序を 1 箇所に持たないと、呼び出し側 (緊急停止 / 試合終了 / 手動への切替) の
        どれか 1 つだけが書き写しを誤り、そこだけが「止めた直後に動き出す」。

        **破棄しない停止は用意しない。** かつては緊急停止だけが破棄を要求しており、
        通常停止と試合終了はまだ拾われていない開始要求を残したまま降りていた。
        その 1 通は次に `run()` が降りた瞬間に `run_forever` が拾うので、症状は
        「STOP を押したのに先頭から全工程を走り切る」になる (試合終了なら、
        フェーズは `finished` なのに機体だけが動き続ける)。選べるようにしておくと、
        経路を 1 つ足すたびに同じ穴が開き直る。
        """
        ctx.sequence.discard_pending_start()
        if ctx.sequence.is_running:
            ctx.sequence.request_stop()

    def set_initial_inactive_motors(self, robot_name: str, motor_names: list[str]) -> None:
        """起動時に有効化できなかったモータを記録する (サーバー起動前に呼ばれる)。

        `_inactive_motors` は緊急停止解除の経路でしか埋まらなかったため、**起動時の
        励磁失敗はログの外にも画面にも出なかった**。操縦者に見えるのは「指令しても
        動かない」だけで、原因を示す表示がどこにも無い。ここへ預けた名前は
        `safety.unenergized_motors` として、解除後の失敗とまったく同じ経路で配信される。
        """
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
        if not await self._ws.send_or_drop(requester, msg):
            await self._ws.drop({requester})

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
        await self._ws.broadcast_json(payload)

    # ------------------------------------------------------------------ #
    #  アクチュエータ動作確認 (Phase 6 段階⑨ — タスク 6-22)
    # ------------------------------------------------------------------ #

    def _motor_check_environment_deny(self) -> str | None:
        """動作確認を起動できない環境側の理由。**シーケンス自身の状態は見ない。**

        優先順:
          1. 試合中 (アクチュエータを一巡させるため試合進行を乱す)
          2. 緊急停止中 (誤発火による駆動を完全に止める)
          3. **どれかの**ロボットで再励磁が in-flight (同じモータへの活性化が競合する)
          4. **どれかの**ロボットが手動操縦モード (制御権の二重取得を防ぐ)
          5. **どれかの**ロボットで通常シーケンス実行中 (同上)

        両ハンドを 1 本で駆動するので、ゲートも全ロボットに対して掛ける。
        片方だけ見ていると、確認中にもう一方が手動で動かされて干渉する。

        WS 経由は handle_command でも同じフェーズ判定を行うが、HTTP POST は
        そこを通らないため本メソッド側にもゲートを置く。
        """
        phase_deny = COMMANDS["motor_check_start"].phase_deny_reason(self.match.phase)
        if phase_deny is not None:
            return phase_deny

        if self._e_stop_active:
            return "緊急停止中のため動作確認を実行できません"

        for name in self._robots:
            if self._is_reenergizing(name):
                # 零点確定 (rotate) は disable → SET_ZERO → enable を伴う。同じモータへ
                # 再励磁の activate_motors が並走すると、`_wait_fresh_feedback` の
                # プローブ (disable) が動作確認側の enable と衝突しうる
                return f"'{name}' の再励磁が完了していないため動作確認を実行できません"

        for name, ctx in self._robots.items():
            if ctx.mode is OperationMode.MANUAL:
                # 手動は操縦者がいつ軸を動かすか分からない。動作確認は決まった順序で
                # 一巡する手順なので、途中で別の指令が割り込むと結果が意味を失う
                return f"'{name}' が手動操縦モードのため動作確認を実行できません"
        for name, ctx in self._robots.items():
            if ctx.sequence.is_running:
                return f"'{name}' の通常シーケンス実行中のため動作確認を実行できません"
        return None

    def _motor_check_pausables(self) -> list[Pausable]:
        """動作確認中に黙らせる周期タスク。**1 つも無い。空を返すのが正しい。**

        動作確認は `move_to` でしか軸を動かさず、`move_to` が出す指令は
        シーケンスと同じ `MotorHandle` を通る。周期タスクはどれもその同じ
        ハンドルの目標を実現する側であって、競合相手ではない。**止めると、
        止めた側が担っていた仕事ごと消える。**

        **M3508 の位置制御ループ**: 動作確認が M3508 を動かす唯一の経路である。
        M3508 は電流指令しか受け付けないので、このループが C620 へ電流を出す
        ことでしか動かない。止めると目標だけが設定されて電流は 1 通も出ず、
        偏差が残ったまま `SequenceTimeoutError` になる (飽和すらしない ——
        PID が 1 周期も回っていないため)。しかも復帰した瞬間に**残った目標へ
        向かって機体が動き出す**ので、操縦者が失敗表示を読んだ直後に動く。

        **目標値再送**: 止めると 2 つが壊れる。
          - **EDULITE 05 / DM3520 の到達を観測できなくなる。** この 2 種は
            フィードバックが問い合わせ駆動で、自分の CAN ID 宛のフレームを受けた
            ときにしか状態を返さない。`AxisHandle.wait_reached` はドライバの
            キャッシュを polling するだけで再送しないので、止めるとそのモータ宛へ
            飛ぶのは `move_to` の指令 1 通だけになり、返るフィードバックも
            **動き出す前の位置 1 通**で以後は更新されない。0deg から 180deg へ
            回す `rotate` は、実際に回りきっても到達判定を通らない
          - **自作モタドラのウォッチドッグが、確認したい当のものを消す。**
            3 枚とも `command_timeout_ms` 500ms で出力を落とす。`conveyor` と
            ポンプの `settle_s` は 0.5s なので、目視・聴音で確認している最中に
            出力が切れる。サーボスロットは現在角で凍結するので、500ms を超える
            移動がある軸は `reached` が永久に立たない

        再送が確認用の指令を上書きすることも無い。`main._build_target_refreshers`
        はロボットのシーケンスと**同じ `MotorHandle` インスタンス**を受け取り、
        `main._wire_motor_check_sequence` はそのハンドルをそのまま統合動作確認の
        `MotorGroup` へ入れる。再送が書き直すのは動作確認自身が設定した目標である。
        `idle_target_value()` のラッチ値が出るのは、そのハンドルが目標を 1 つも
        持たないときだけで、中身は「今の姿勢を保て」でしかない。

        0x200 の奪い合いも起きない。動作確認中は他の指令経路 (通常シーケンス実行・
        手動モード) が `MotorCheckController.deny_reason()` の排他で塞がれている。

        `Pausable` の仕組み自体は残す。`safety.position_loops[].paused` /
        `safety.target_refreshers[].paused` は WS 契約に載っており、緊急停止解除の
        再励磁のように「送信経路を一時的に別の主が握る」用途は今後も起こりうる。
        """
        return []

    async def _motor_check_post(self, request: web.Request) -> web.Response:
        """POST /motor_check: 動作確認の起動エンドポイント。

        両ハンド統合の 1 本なので robot を取らない。起動成功時は即時 200 を返し、
        進捗は WS 経由で配信する。拒否時は 409 を返し、理由も一緒に返す
        (WS 側にも同じ理由が状態として流れる)。
        """
        started = await self._motor_check.start()
        if not started:
            return web.json_response(
                {"started": False, "reason": self._motor_check.error}, status=409
            )
        return web.json_response({"started": True}, status=200)

    async def _motor_check_get(self, request: web.Request) -> web.Response:
        """GET /motor_check: 動作確認の現在状態 (WS が使えない環境向けの代替経路)。"""
        return web.json_response(self._motor_check.payload(), status=200)

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
                # 動作確認の進捗はここに相乗りさせる。専用の配信ループを増やすと
                # `WsHub.fanout` の約束事 (送信ごとの上限・切り離しの後始末) を守る経路が
                # もう 1 本増える。変化が無ければ 1 通も流れない
                await self._motor_check.publish()
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

        if self._reactivating:
            # 再励磁の最中は、まだ解除フレームの届いていない基板が「停止中」を
            # 報告し続ける。それを信じると解除した瞬間にサーバーが自分で止め直し、
            # **二度と解除できない機体**になる。判定に使う
            # `_board_e_stop_ignore_before` が確定するのも再励磁を終えた後
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

        if not self._ws.has_clients:
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

        await self._ws.fanout([*state_messages, *change_events])

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
                raw = server_dryrun.motor_state(robot_name, motor_name)
            else:
                s = motor.state
                raw = {
                    "pos": s.position,
                    "vel": s.velocity,
                    "torque": s.current,
                    "temp": s.temperature,
                }
            # 測る手段の無い項目は None で配る。**dry-run にも同じ規則を通す** ——
            # 擬似値を作ってよいのは実機が測れる項目だけで、DC 基板に温度や速度を
            # 作ると机上で確かめている画面が実機と別物になる
            motors[motor_name] = _measured_only(raw, motor.telemetry)
            # 指令値も dry-run 分岐の外で足す。**擬似値を作ってはならない** ——
            # 測れないモータの画面が指令値だけを頼りにしている以上、机上で見えている
            # 数字が「PC が実際に送った値」でなければ確かめたい対象そのものが消える
            motors[motor_name].update(self._motor_command_state(robot_name, motor_name))

        # snapshot が未指定 (テストや単独呼び出し) の場合はその場で計算する。
        # _broadcast_state からの呼び出しは事前計算済みのものを使い回して二重計算を避ける。
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
            # UI に step_index/total_steps から実行状態を推測させないため、
            # シーケンス側が持っている実行フラグをそのまま配信する
            "running": progress["running"],
            "steps": progress.get("steps", []),
            # 止まった理由。到達タイムアウト・左右ずれ・零点確定失敗はステップ単位の
            # try で握られるので、ここに載せない限り journal 以外どこにも出ない
            # (画面は「待機中 — START で開始」と描くだけになる)。平常時は null
            "last_error": progress["last_error"],
            "motors": motors,
            "sensors": self._sensor_states(robot_name),
            "e_stop_active": self.e_stop_active,
            "health": snapshot_dict,
            "safety": self._safety_state(robot_name),
            "manual": self._manual_state(robot_name),
        }

    def _sensor_states(self, robot_name: str) -> dict[str, dict]:
        """自作基板のセンサ入力 (原点スイッチ) の接触状態。

        **接触は異常ではない** (`GenericDriver.sensor_active`) ので、`active` は
        情報として配る。異常なのは `stale` の方で、ヘルス判定 (`CANManager.health`)
        は同じ鮮度でセンサを STALE に倒している。

        配る理由は 3 つ:

        1. `config/checklist.yaml` の `origin_sensor_react` (原点センサに 1 本ずつ
           触れて反応を確認する) は、確認する手段が画面に無いまま項目だけがあった。
           操縦者は `candump` を打たない限り反応を確かめられない
        2. **未配線・極性違いのセンサは STALE にならない。** 基板は役割が
           TouchSensor なら配線の有無に関わらず FEEDBACK を送り、`INPUT_PULLUP` の
           負論理で「接触なし」を報告し続けるので、ヘルスも平常のままになる。
           押してみる以外に検出手段が無い
        3. 零点確定は「当たるまで動かす」動作 (`lib/sequence/homing.py`) なので、
           センサが死んでいると探索距離いっぱいまで機構を押し込む

        鮮度のしきい値は `HealthThresholds` から来た 1 つだけを使う。ここに別の
        既定値を置くと、config を直しても画面の判定だけが古い境界のまま残る。
        """
        ctx = self._robots[robot_name]
        freshness = FeedbackFreshness(
            ctx.can_manager.last_feedback_at, timeout_ms=self._health.feedback_timeout_ms
        )
        # 1 周期に 1 回だけ取る。センサごとに取り直すと、同じ配信の中で別々の
        # 瞬間を基準にした鮮度が並ぶ
        now = freshness.now()

        sensors: dict[str, dict] = {}
        for sensor_name, sensor in ctx.can_manager.sensors.items():
            if self._dry_run:
                sensors[sensor_name] = server_dryrun.sensor_state(robot_name, sensor_name)
                continue
            sensors[sensor_name] = {
                # 接触を報告できるのは自作基板のセンサスロットだけ (仕様書 §5.2)。
                # config の `sensors:` は driver を選べないので実機では必ず該当するが、
                # **報告する手段を持たないドライバを False で埋めてはならない** ——
                # 「触れていない」と区別が付かなくなる (`_measured_only` と同じ扱いで
                # null へ倒し、UI には「—」を描かせる)
                "active": sensor.sensor_active if isinstance(sensor, GenericDriver) else None,
                "stale": freshness.is_stale(sensor_name, now),
            }
        return sensors

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
        await self._ws.close_all()
