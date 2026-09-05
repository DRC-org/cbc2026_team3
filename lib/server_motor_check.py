"""セッティングタイムのアクチュエータ動作確認の統括。

駆動そのものは `sequences/motor_check.py` の 1 本のシーケンスが持つ。ここが持つのは
その周りの判断 —— **起動してよいか / 排他を取れているか / 何を配るか** —— で、
サーバー本体から切り出してある。

**両ハンドを 1 本のシーケンスで駆動する。** かつては機体ごとに独立した確認を
持っていたが、2 つを同時に起動できるため両機が同時に動き、可動域の重なる位置で
干渉しうる。順序を 1 本に固定すれば、いつ何が動くかがシーケンスの並びから読める。

環境側の条件 (フェーズ・緊急停止・各ロボットの制御権) はサーバーが答える。
本クラスは ``environment_deny`` にそれを預け、自分の 2 条件 (未登録 / 実行中) と
合わせて 1 つの拒否理由にする —— **可否の判定はここ 1 箇所にしかない。**
UI は ``blocked_reason`` を表示するだけで、フェーズや緊急停止から導出し直さない。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Protocol

from lib.match_state import Court
from lib.sequence.engine import Sequence

logger = logging.getLogger(__name__)

__all__ = ["MotorCheckController", "Pausable"]


class Pausable(Protocol):
    """動作確認中に黙らせる周期タスク。

    何を止めるかは `RobotServer._motor_check_pausables` だけが決める。ここで
    種別を数え上げると、止める / 止めないの判断が 2 箇所に分かれる。
    """

    async def pause(self, *, reason: str) -> None: ...

    def resume(self) -> None: ...


class MotorCheckController:
    """動作確認の起動・中断・状態配信。

    Args:
        environment_deny: 環境側の拒否理由を返す (フェーズ・緊急停止・手動モード・
            通常シーケンス実行中)。許可なら None
        pausables: 起動時に黙らせる周期タスク。**全ロボットぶんを渡すこと** ——
            1 本のシーケンスが両機を動かすので、片方だけ止めると残った側の
            再送が同じモータの指令を奪い合う。**M3508 の位置制御ループは
            含まれない** (理由は `RobotServer._motor_check_pausables`)
        is_e_stop_active: 今この瞬間モータを動かしてよいか
        broadcast: 状態 1 通の配信口
    """

    def __init__(
        self,
        *,
        environment_deny: Callable[[], str | None],
        pausables: Callable[[], list[Pausable]],
        is_e_stop_active: Callable[[], bool],
        broadcast: Callable[[dict], Awaitable[None]],
    ) -> None:
        self._environment_deny = environment_deny
        self._pausables = pausables
        self._is_e_stop_active = is_e_stop_active
        self._broadcast = broadcast

        # main.py が set_sequence で注入する。どのロボットにも属さない
        # (両ハンドのアクチュエータを 1 つの順序で駆動するため、RobotContext の
        # 下に置くと「どちらの機体のものか」が答えられなくなる)
        self._sequence: Sequence | None = None
        # 実行タスク。二重起動の判定はこの生死で行う — シーケンスの `is_running` は
        # タスク生成から run() 開始までのあいだ False で、そこを素通しすると
        # 2 本目が走り出して pause/resume が食い違う (入れ子カウントを持たないため、
        # 先に終わった側の resume がもう一方の駆動中に送信を再開させる)
        self._task: asyncio.Task[None] | None = None
        # 中断要求。**`Sequence` の停止イベントとは別に持つ必要がある。**
        # `Sequence.run()` は冒頭で停止イベントを clear するので、タスク生成から
        # run() 開始までのあいだに届いた中断はそこで消える。その窓で緊急停止や
        # 操縦者の中断が入ると「止めたはずなのに全アクチュエータが順に駆動される」
        self._abort_requested: bool = False
        # 直近の拒否・失敗理由。実行状態と同じ 1 通に載せて配信する
        self._error: str | None = None
        # 前回配信した内容。変化したときだけ送る (停止中は何も流れない)
        self._last_payload: dict | None = None

    # ------------------------------------------------------------------ #
    #  登録と参照
    # ------------------------------------------------------------------ #

    def set_sequence(self, sequence: Sequence, *, court: Court) -> None:
        self._sequence = sequence
        sequence.set_court(court)

    def set_court(self, court: Court) -> None:
        if self._sequence is not None:
            self._sequence.set_court(court)

    @property
    def sequence(self) -> Sequence | None:
        return self._sequence

    @property
    def error(self) -> str | None:
        return self._error

    @property
    def running(self) -> bool:
        """統合動作確認が走っているか。

        シーケンスの `is_running` ではなく実行タスクの生死で見る。タスク生成から
        `run()` 開始までのあいだ `is_running` は False で、そこを素通しすると
        2 本目が走り出し、pause/resume が食い違う。
        """
        return self._task is not None and not self._task.done()

    def deny_reason(self) -> str | None:
        """動作確認を起動できない理由。起動してよければ None。

        拒否条件の優先順:
          1. シーケンス未登録 (位置定数を読めていない構成)
          2〜5. 環境側 (試合中 / 緊急停止中 / どれかのロボットが手動 or 実行中)
          6. 既に動作確認実行中 (二重起動の防止)

        **可否の判定はここにしかない。** UI は `blocked_reason` を表示するだけで、
        フェーズや緊急停止から導出し直してはならない。
        """
        if self._sequence is None:
            return "動作確認シーケンスが読み込まれていません"

        environment = self._environment_deny()
        if environment is not None:
            return environment

        if self.running:
            return "既に動作確認を実行中です"
        return None

    # ------------------------------------------------------------------ #
    #  起動と中断
    # ------------------------------------------------------------------ #

    async def start(self) -> bool:
        """統合動作確認シーケンスを起動する。拒否時は False を返す。"""
        deny = self.deny_reason()
        if deny is not None:
            await self.report_error(deny)
            return False

        sequence = self._sequence
        assert sequence is not None  # deny_reason が保証する

        # **必ず先頭から流す。** 中断した位置から再開すると、そこまでの姿勢が
        # 前提になっているステップを飛ばしたまま次を動かすことになる
        await sequence.reset()
        self._error = None
        self._abort_requested = False

        # 何を黙らせるかは `RobotServer._motor_check_pausables` が 1 箇所で決める。
        # **M3508 の位置制御ループはそこに含まれない** —— 動作確認の `move_to` は
        # そのループを通ってしか M3508 を動かせないので、止めると電流が 1 通も
        # 出ないまま到達待ちがタイムアウトする
        pausables = self._pausables()

        async def _run() -> None:
            try:
                for pausable in pausables:
                    await pausable.pause(reason="動作確認")
                if self._is_e_stop_active():
                    # pause() は送信中の 1 周期ぶんブロックしうる。その窓のあいだに
                    # 緊急停止が入ったら 1 台も駆動せずに降りる (起動判定は既に
                    # 過去のもので、今この瞬間モータを動かしてよいかを答えていない)
                    await self.report_error("緊急停止中のため動作確認を中止しました")
                    return
                if self._abort_requested:
                    # **`sequence.run()` に任せてはならない。** run() は冒頭で停止
                    # イベントを clear するので、ここまでに届いた中断はそこで消え、
                    # 「止めたはずなのに全アクチュエータが順に駆動される」ことになる
                    await self.report_error("動作確認を中断しました")
                    return
                await sequence.run()
                # **失敗はここでしか受け取れない。** 到達タイムアウト・左右ずれ・
                # 零点確定失敗はどれも `Sequence.run()` のステップ単位 try が握るので、
                # 下の `except` は構造上決して発火しない。拾わないと、失敗した確認が
                # `error:None` / `step_index:0` のまま「一度も実行していない」と同じ
                # 表示に戻り、指差喚呼の「動作確認 完了」がその誤表示のまま付く
                failure = sequence.last_error
                if failure is not None:
                    await self.report_error(
                        f"ステップ '{failure.label}' で失敗しました: {failure.message}"
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - 防御的
                logger.exception("動作確認エラー: %s", exc)
                await self.report_error(str(exc))
            finally:
                # 中断・例外・キャンセルのいずれで抜けても必ず復帰させる。
                # 止まったままだと昇降軸が保持電流を失って落下し、
                # 再送が止まったままだとコンベアが 500ms で動かなくなる
                for pausable in pausables:
                    pausable.resume()

        self._task = asyncio.create_task(_run())
        return True

    def abort(self) -> None:
        """動作確認を中断する。走っていなくても要求は残す。

        `cancel()` ではなく通常停止で降ろす。走行中のステップは完了まで待つので、
        指令の途中でタスクが消えて「半分だけ動いた軸」が残ることがない。

        **走っているかを条件にしない。** 起動タスクを作ってから `run()` に入るまでの
        窓では `is_running` が False で、そこで押された停止をすり抜けた動作確認が
        完走して全アクチュエータを駆動する。要求はフラグに残し、`start()` の中の
        実行タスクが駆動を始める直前に見る。
        """
        self._abort_requested = True
        if self._sequence is not None:
            self._sequence.request_stop()

    # ------------------------------------------------------------------ #
    #  配信
    # ------------------------------------------------------------------ #

    def payload(self) -> dict:
        """動作確認の状態。**進捗も結果も拒否理由も 1 通で運ぶ。**

        かつては progress / record / done / error の 4 種類を別々に配っていた。
        受け取る側は 4 通を継ぎ合わせて 1 つの状態を組み立てることになり、
        途中の 1 通を取りこぼすと画面と機体が食い違ったまま復旧しない
        (再送も無いので、リロードするまで直らない)。

        ステップ表 (`steps`) を毎回載せるのは、途中から繋いだクライアントにも
        同じ 1 通で全体が伝わるようにするため。
        """
        # **キー集合は 1 箇所でしか作らない。** シーケンス未登録の分岐と通常分岐で
        # 別々に組み立てると、キーを足したときに片方へ書き忘れられる。受け取る側は
        # 「1 通で全体が伝わる」前提で描くので、欠けたキーは古い状態のまま固まる。
        #
        # 未登録のときも拒否理由を載せる —— 「読み込まれていません」という理由自体が
        # その分岐から出るので、捨てると押しても何も起きない画面になる
        payload = {
            "type": "motor_check_state",
            "available": self._sequence is not None,
            "blocked_reason": self.deny_reason(),
            "running": False,
            "current_step": None,
            "step_index": 0,
            "total_steps": 0,
            "steps": [],
            # 表示 1 行。拒否理由も失敗理由もここへ集約する
            "error": self._error,
            # どのステップで失敗したか。平常時は null で、`error` と違って
            # 「どこまで確認できたか」を機械的に読める形で持つ
            "last_error": None,
            # 構成に無い軸を指令するため登録しなかったステップ。**空でも必ず載せる。**
            # 除外を配信しないと、サブハンド不在でステップが減っているのか、本番構成
            # なのに config の書き忘れで減っているのかを操縦者が画面で区別できない
            # (どちらも「全ステップ成功」として同じに見える)
            "excluded_steps": [],
        }
        if self._sequence is None:
            return payload

        progress = self._sequence.progress
        payload["running"] = self.running
        payload["current_step"] = progress["current_step"]
        payload["step_index"] = progress["step_index"]
        payload["total_steps"] = progress["total_steps"]
        payload["steps"] = progress["steps"]
        payload["last_error"] = progress["last_error"]
        payload["excluded_steps"] = [
            excluded.to_dict() for excluded in self._sequence.excluded_steps
        ]
        return payload

    async def report_error(self, message: str) -> None:
        """拒否・失敗の理由を保持し、状態として配信する。

        次の起動が成功するまで消さない。押した直後に消えると、操縦者は
        「押したのに何も起きなかった」としか読み取れない。
        """
        self._error = message
        logger.warning("動作確認: %s", message)
        await self.publish()

    async def publish(self) -> None:
        """変化したときだけ配信する。

        テレメトリと違って停止中は何も変わらないので、毎ティック流すと
        「変化時のみ配信」を前提にした UI 側の再描画抑制が効かなくなる。
        """
        payload = self.payload()
        if payload == self._last_payload:
            return
        self._last_payload = payload
        await self._broadcast(payload)
