"""制御層の常駐タスクが共有する周期実行の土台。

``M3508PositionLoop`` (200Hz) / ``SyncMonitor`` (50Hz) / ``GenericTargetRefresher``
(20Hz) は「一定周期で回り続け、例外が出ても止まらない」という同じ骨格を持つ。
かつては 3 クラスがそれぞれ ``run`` / ``start`` / ``stop`` を書いており、二重
``start()`` の扱い (例外 / 無視)、``stop()`` の cancel の有無、停止要求のクリア場所が
食い違っていた。同じパッケージの兄弟クラスで作法が違うと、片方の知識でもう片方を
触ったときに事故る (「止めたつもりが止まっていない」が最も危ない) ため、
ライフサイクルはここに一本化する。

周期は「処理 → sleep(interval)」ではなく次回起床時刻を絶対時刻で管理する。
後置 sleep では実周期が ``interval + 処理時間`` になり、公称 50Hz を前提に
「2 サンプル = 40ms なら機構破損に間に合う」と書いている偏差監視の応答が
負荷に比例して伸びてしまう (lib/axis_sync.py のモジュール docstring を参照)。
"""

from __future__ import annotations

import abc
import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

__all__ = [
    "LOG_THROTTLE_S",
    "LogThrottle",
    "PausablePeriodicTask",
    "PeriodicTask",
]

# 同一原因のログを毎周期出すと 200Hz でログが溢れ、本当に読みたい 1 行が流れる
LOG_THROTTLE_S = 1.0

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
    """アクチュエータ動作確認と送信経路を奪い合うタスクの基底。

    動作確認は同じバス・同じモータへ自前の指令を出すため、常駐タスクが並行して
    送り続けると互いのフレームを打ち消し合い、健全なモータが FAILED になる
    (M3508 なら 0x200 のスロット、自作モタドラなら SET_TARGET)。排他は必ず
    「常駐タスク側が黙る」方向で取る (動作確認は常に短時間 + 通常制御外)。

    ``pause()`` は戻り値時点で在庫の 1 周期も送信済みでないことを保証する。
    これが無いと、送信途中の周期が動作確認の指令を上書きしうる。
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
