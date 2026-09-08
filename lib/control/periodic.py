"""制御層の常駐タスクが共有する周期実行の土台。

``M3508PositionLoop`` (200Hz) / ``SyncMonitor`` (50Hz) / ``GenericTargetRefresher``
(20Hz) は「一定周期で回り続け、例外が出ても止まらない」という同じ骨格を持つ。
各クラスが自前で ``run`` / ``start`` / ``stop`` を書くと二重 ``start()`` の扱いや
``stop()`` の cancel の有無が食い違い、片方の知識でもう片方を触ったときに
「止めたつもりが止まっていない」が起きるので、ライフサイクルはここに一本化する。

周期は「処理 → sleep(interval)」ではなく次回起床時刻を絶対時刻で管理する
(後置 sleep では実周期が ``interval + 処理時間`` になり、偏差監視の時間予算が
負荷に比例して伸びる)。

実周期の実測もここに一本化する (継承先へ書き写すと、書き忘れた 1 つだけが乱れを
検知できないまま残る)。測るのは「連続する 2 回の tick 開始時刻の差」で、起床の
遅れと処理落ちを区別せず 1 つの数字に落とす —— 偏差監視の時間予算も
`trajectory.py` の停止距離も実周期そのものに依存するので、原因の内訳より
「実際にどれだけ遅れたか」が要る。サンプル列は持たず最大値・超過回数だけを O(1) で
積む: **tick の仕事を定数時間に保たないと、測っている当人が周期を伸ばす側になる。**
"""

from __future__ import annotations

import abc
import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

__all__ = [
    "JITTER_OVERRUN_MARGIN",
    "LOG_THROTTLE_S",
    "LogThrottle",
    "PausablePeriodicTask",
    "PeriodicTask",
]

# 同一原因のログを毎周期出すと 200Hz でログが溢れ、本当に読みたい 1 行が流れる
LOG_THROTTLE_S = 1.0

# 実周期の「公称値からの超過分 / 公称周期」がこの値を上回ったら乱れとして数える。
# 0.5 = 公称の 1.5 倍で発火する (名前を倍率でなく超過分の割合にしてあるのは、
# 宣言の値と名前を一致させるため)。`interval_s` からの相対値にするのは、
# 200/50/20Hz の 3 種を跨ぐしきい値を絶対値で持つと読み手が都度換算するため。
# 値の根拠: 50Hz の偏差監視は「2 サンプル = 40ms で機構破損に間に合う」が前提
# (`lib/axis_sync.py`) で、1.5 倍 = 30ms は既にその予算の 75% を単独で食う。
JITTER_OVERRUN_MARGIN = 0.5

SleepFunc = Callable[[float], Awaitable[None]]
TimeSource = Callable[[], float]


class LogThrottle:
    """原因の種類ごとにログを間引く。

    間引きの基準に単調クロックを使うのは、壁時計だと NTP の時刻補正で間引き窓が
    飛び、障害の最中にログが溢れる (または 1 行も出ない) ことがあるため。
    """

    def __init__(
        self,
        logger: logging.Logger,
        *,
        time_source: TimeSource = time.monotonic,
        interval_s: float = LOG_THROTTLE_S,
    ) -> None:
        self._logger = logger
        self._time_source = time_source
        self._interval_s = interval_s
        self._last_at: dict[str, float] = {}

    def exception(self, key: str, message: str, *args: object) -> None:
        """処理中の例外を間引いて記録する。トレースバックは必ず残す。"""
        now = self._time_source()
        last = self._last_at.get(key)
        if last is not None and now - last < self._interval_s:
            return
        self._last_at[key] = now
        self._logger.error(message, *args, exc_info=True)

    def warning(self, key: str, message: str, *args: object) -> None:
        """例外ではない警告を間引いて記録する (トレースバックは付けない)。

        周期の乱れは例外を伴わない (tick は正常に完了したが遅かっただけ) ので、
        ``exception()`` の ``exc_info=True`` は使えない。``key`` の名前空間は
        ``exception()`` と共有しているので、呼び出し側で衝突しない名前を選ぶこと。
        """
        now = self._time_source()
        last = self._last_at.get(key)
        if last is not None and now - last < self._interval_s:
            return
        self._last_at[key] = now
        self._logger.warning(message, *args)


class PeriodicTask(abc.ABC):
    """一定周期で ``_tick()`` を呼び続ける常駐タスクの基底。

    サブクラスに要求するのは ``_tick()`` (1 周期分の処理) と ``_label()``
    (ログに出す識別子) だけ。周期処理で例外が出てもループを抜けないのは
    3 者共通の要件である (位置制御ループなら指令断で C620 が惰走し、同期監視なら
    防護が丸ごと消え、目標値再送ならファームのウォッチドッグで 500ms 後に機体が
    止まる)。抜けない代わりに ``_on_tick_error()`` で必ず記録に残す。
    """

    def __init__(
        self,
        *,
        interval_s: float,
        time_source: TimeSource = time.monotonic,
        sleep: SleepFunc = asyncio.sleep,
        logger: logging.Logger | None = None,
    ) -> None:
        """
        Args:
            interval_s: 公称周期 [s]
            time_source: 次回起床時刻の計算に使う単調クロック
            sleep: 周期待ちに使う関数 (テストで差し替え可能)
            logger: 間引きログの出力先 (既定はサブクラスのモジュールロガー)
        """
        self._interval_s = interval_s
        self._time_source = time_source
        self._sleep = sleep
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._logger = logger if logger is not None else logging.getLogger(type(self).__module__)
        self._log = LogThrottle(self._logger, time_source=time_source)

        # 実周期のジッタ計測。サンプル列は持たず O(1) の集計だけを積む
        # (`_last_tick_at` は前回 tick の開始時刻。`start()` で None に戻す —
        # 停止していた間の空白を「乱れ」として数えないため)。
        self._last_tick_at: float | None = None
        self._jitter_overrun_count = 0
        self._worst_jitter_s = 0.0
        self._jitter_threshold_s = interval_s * JITTER_OVERRUN_MARGIN

    # ------------------------------------------------------------------ #
    #  サブクラスが実装する
    # ------------------------------------------------------------------ #

    @abc.abstractmethod
    async def _tick(self) -> None:
        """1 周期分の処理。"""

    @abc.abstractmethod
    def _label(self) -> str:
        """ログに出すこのタスクの識別子 (例: ``位置制御ループ (bus=can_m3508)``)。"""

    async def _on_run_start(self) -> None:  # noqa: B027  (任意フック。既定は何もしない)
        """ループに入る直前のフック (時刻基準の取り直しなど)。"""

    async def _on_tick_error(self) -> None:
        """周期処理で例外が出たときのフック。既定は間引きログのみ。"""
        self._log.exception("tick", "%s の周期処理で例外", self._label())

    async def _on_run_exit(self) -> None:  # noqa: B027  (任意フック。既定は何もしない)
        """ループを降りるときのフック。異常終了・キャンセルでも必ず通る。"""

    def _observe_tick_start(self, now: float) -> None:
        """実周期を測り、超過回数と最悪値だけを O(1) で積む。

        前回 tick からの経過 (= 実周期) と公称 ``interval_s`` の差を「乱れ」とする。
        起床が遅れた分と tick 自身の処理が長すぎた分を区別しないのは、
        `SyncMonitor` の時間予算にとってはどちらも同じ「次の周期までに終わらなかった」
        だからである。初回 (直前の tick が無い) は比較対象が無いので何もしない。
        """
        last = self._last_tick_at
        self._last_tick_at = now
        if last is None:
            return

        jitter = (now - last) - self._interval_s
        if jitter > self._worst_jitter_s:
            self._worst_jitter_s = jitter
        if jitter > self._jitter_threshold_s:
            self._jitter_overrun_count += 1
            self._log.warning(
                "jitter",
                "%s の実周期が乱れています (実測 %.1fms / 公称 %.1fms)",
                self._label(),
                (now - last) * 1000.0,
                self._interval_s * 1000.0,
            )

    @property
    def jitter_overrun_count(self) -> int:
        """実周期が公称値を大きく (``JITTER_OVERRUN_MARGIN`` 超) 上回った回数。"""
        return self._jitter_overrun_count

    @property
    def worst_jitter_s(self) -> float:
        """観測した実周期の超過分 [s] の最大値 (しきい値未満の乱れも含む)。

        **超過が 1 件も無くても 0 とは限らない** —— ``jitter_overrun_count`` の 0 とは
        別物である。実機の ``asyncio.sleep`` は必ず数百 us オーバーシュートするので、
        公称どおりに回っている機体でもここには小さな正の値が入る。しきい値
        (``JITTER_OVERRUN_MARGIN``) が実機未検証である以上、それを決めるための
        唯一の観測値がこの値なので、更新を超過ブランチの内側へ移してはならない。
        """
        return self._worst_jitter_s

    def log_jitter_summary(self) -> None:
        """直前の試合ぶんの実周期を journal へ 1 行残す。呼ぶのは match_finish だけ。

        **超過 0 件でも必ず出す。** しきい値 (``JITTER_OVERRUN_MARGIN``) は理屈で
        導いた値で実機未検証であり、それを決めるための唯一の観測値が
        ``worst_jitter_s`` である。「超過したときだけ出す」にすると、公称の 1.4 倍で
        常時走っている機体 —— 200Hz が 143Hz に落ち、`lib/control/trajectory.py` の
        停止距離も `SyncMonitor` の 40ms 予算も既に崩れている状態 —— で journal に
        1 行も出ず、読み手は「警告 0 件 = 乱れていない」と読む。しきい値を決める
        ためにしきい値を超えている必要がある、という循環になる。
        1 試合あたり (位置制御 + 同期監視 + 目標値再送) x 2 ロボット = 6 行程度なので
        氾濫しない。**超過 0 件かどうかは文言で読み分けられるようにしてある。**

        画面・WS 配信には出していない (しきい値が実機未検証。現況は
        ``docs/checks_and_health.md`` の「3 層はどれも『公称周期どおりに回っている』
        ことが前提 — その乱れを測る」節、経緯は ``docs/impl_plan.md`` の限界表)。
        """
        if self._jitter_overrun_count == 0:
            self._logger.info(
                "%s の実周期: しきい値超過なし (最悪の遅れ %.1fms / 公称周期 %.1fms)",
                self._label(),
                self._worst_jitter_s * 1000.0,
                self._interval_s * 1000.0,
            )
        else:
            self._logger.info(
                "%s の実周期: しきい値超過 %d 回 (最悪の遅れ %.1fms / 公称周期 %.1fms)",
                self._label(),
                self._jitter_overrun_count,
                self._worst_jitter_s * 1000.0,
                self._interval_s * 1000.0,
            )

    def reset_jitter_stats(self) -> None:
        """乱れの記録を 0 に戻す。ログは出さない (集計 1 行は ``log_jitter_summary``)。

        呼び口は `lib/server.py` の 2 箇所 —— match_finish (集計を残した直後) と
        match_start (前縁リセット。準備中に踏んだぶんを洗い流す)。

        **回数と最悪値は必ず両方一緒に落とす。** 片方だけ残すと「超過 0 回なのに
        最悪の遅れだけ残る」不整合になり、次の集計 1 行が前の試合の数字を混ぜて
        名乗る。これはリセットの不変条件であって、ログを出すかどうかの話ではない。
        ``_last_tick_at`` は触らない —— ``None`` に戻すと直後の 1 tick 分の乱れ
        検知を取りこぼす (`start()` が捨てる「停止していた空白」とは違い、
        ここはタスクが動き続けたままの呼び出しなので空白が無い)。
        """
        self._jitter_overrun_count = 0
        self._worst_jitter_s = 0.0

    # ------------------------------------------------------------------ #
    #  ライフサイクル
    # ------------------------------------------------------------------ #

    async def run(self) -> None:
        """停止要求まで回し続ける。

        停止要求をここでクリアしないのは、タスク起動前に ``request_stop()`` が
        呼ばれた場合にその要求を取りこぼして走り続けてしまうため。再起動時の
        クリアは ``start()`` が行う。
        """
        await self._on_run_start()
        next_at = self._time_source() + self._interval_s
        try:
            while not self._stop_event.is_set():
                self._observe_tick_start(self._time_source())
                try:
                    await self._tick()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    await self._on_tick_error()

                delay = next_at - self._time_source()
                if delay > 0.0:
                    next_at += self._interval_s
                else:
                    # 1 周期ぶん以上遅れた。遅れを取り戻そうと待たずに詰めて回すと
                    # 復帰直後に処理が集中して更に遅れる。位相を捨てて数え直す
                    next_at = self._time_source() + self._interval_s
                    delay = 0.0
                await self._sleep(delay)
        finally:
            await self._on_run_exit()

    def start(self) -> None:
        """``run()`` をバックグラウンドタスクとして起動する。

        二重起動を黙って無視せず例外にするのは、「起動したつもり」で止まっている
        タスクを試合中に発見できないため。停止済みからの再起動は通す。
        """
        if self.is_running:
            raise RuntimeError(f"{self._label()} は既に実行中です")
        self._stop_event.clear()
        # 停止していた間の空白を実周期の乱れとして数えないため、直前 tick の
        # 記録を捨てる (次の tick は「初回」として扱われ、比較対象を持たない)。
        self._last_tick_at = None
        self._task = asyncio.create_task(self.run())

    def request_stop(self) -> None:
        """次の周期でループを抜けるよう要求する (同期)。"""
        self._stop_event.set()

    async def stop(self) -> None:
        """ループを止めてタスクの終了を待つ。

        ``cancel()`` は使わない。``_on_run_exit()`` で 0 電流フレームを送るような
        「降りる前にやることがある」タスクがあり、キャンセルするとその await が
        中断されて止め損なう。待ち時間は最大 1 周期で済む。

        既に例外で死んでいるタスクの例外はここで再送出しない。``main()`` の
        ``finally`` は「全ループを止める → 全 CAN を落とす」を素の for で並べており、
        1 つが送出した時点で以降の後始末が丸ごと飛ぶ (2 台目のロボットのバスが
        開いたまま残る)。止める処理が止まる形は安全側ではない。死因はループ側の
        間引きログに残っているが、``stop()`` から見える最後の痕跡としてここでも記録する。
        """
        self.request_stop()
        task = self._task
        self._task = None
        if task is None:
            return
        try:
            await task
        except asyncio.CancelledError:
            # 外からキャンセルされた場合。停止要求としては成立している
            pass
        except Exception:
            self._logger.exception("%s は既に異常終了していました", self._label())

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def set_sleep(self, sleep: SleepFunc) -> None:
        """周期待ち関数を差し替える (テスト用)。"""
        self._sleep = sleep


class PausablePeriodicTask(PeriodicTask):
    """送信経路を一時的に別の主へ明け渡せる周期タスクの基底。

    ``pause()`` は戻り値時点で在庫の 1 周期も送信済みでないことを保証する。
    これが無いと、送信途中の周期が明け渡した先の指令を上書きしうる。
    排他は必ず「常駐タスク側が黙る」方向で取る。

    **アクチュエータ動作確認はこの口を使わない。** 動作確認は `move_to` でしか
    軸を動かさず、その指令はシーケンスと同じ ``MotorHandle`` を通るので、
    ここに並ぶタスクはどれも目標を実現する側であって競合相手ではない
    (`RobotServer._motor_check_pausables`)。``paused`` は WS 契約
    (``safety.position_loops[].paused`` / ``safety.target_refreshers[].paused``) に
    載っており、外すと UI まで波及する。
    """

    def __init__(
        self,
        *,
        interval_s: float,
        time_source: TimeSource = time.monotonic,
        sleep: SleepFunc = asyncio.sleep,
        logger: logging.Logger | None = None,
    ) -> None:
        super().__init__(interval_s=interval_s, time_source=time_source, sleep=sleep, logger=logger)
        self._paused = False
        self._step_lock = asyncio.Lock()

    @abc.abstractmethod
    async def _step_locked(self) -> None:
        """``_step_lock`` を保持した状態で行う 1 周期分の処理。"""

    def _on_resume(self) -> None:
        """一時停止解除時のフック (積分のリセットなど)。"""

    async def step(self) -> None:
        """1 周期分の処理を行う。``run()`` から呼ばれるほか、テストから直接駆動できる。"""
        async with self._step_lock:
            await self._step_locked()

    async def _tick(self) -> None:
        await self.step()

    @property
    def is_paused(self) -> bool:
        return self._paused

    async def pause(self, *, reason: str = "") -> None:
        """送信を止める。戻り値時点で在庫の周期も送信済みでないことを保証する。"""
        async with self._step_lock:
            if self._paused:
                return
            self._paused = True
            self._logger.info(
                "%s を一時停止%s",
                self._label(),
                f" (理由={reason})" if reason else "",
            )

    def resume(self) -> None:
        """一時停止を解除する。

        同期メソッドにしてあるのは、動作確認側の ``finally`` から待ち合わせなしで
        必ず呼べるようにするため。復帰に失敗するとリフトが保持電流を失い、
        コンベアは以後 500ms で止まる機体になる。
        """
        if not self._paused:
            return
        self._on_resume()
        self._paused = False
        self._logger.info("%s を再開", self._label())
