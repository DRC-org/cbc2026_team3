from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Sequence

from lib.sequence.motors import MotorHandle

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_INTERVAL_S",
    "FIRMWARE_COMMAND_TIMEOUT_S",
    "GenericTargetRefresher",
]

# 自作モータドライバのファームが持つコマンドウォッチドッグの猶予
# (docs/motor_driver_can_protocol.md §5.1 の command_timeout_ms 既定値)
FIRMWARE_COMMAND_TIMEOUT_S = 0.5

# 再送周期 20Hz。ウォッチドッグの猶予 500ms に対して 10 倍の余裕があり、
# CAN の取りこぼしや asyncio のジッタで 9 回連続して落ちても出力は止まらない。
# 位置決めではなく「生存通知」なので、これ以上速くしてもバスを埋めるだけ
DEFAULT_INTERVAL_S = 0.05

# 同一原因のログを毎周期出すとバスが荒れている間ログが溢れるため間引く
_LOG_THROTTLE_S = 1.0

EStopChecker = Callable[[], bool]
SleepFunc = Callable[[float], Awaitable[None]]


class GenericTargetRefresher:
    """自作モータドライバ宛の目標値を低頻度で再送し続ける非同期タスク。

    ファームは ``command_timeout_ms`` (既定 500ms) の間 SET_TARGET を 1 通も
    受け取らないと出力を止める。PC が落ちてもコンベアが回り続けないための
    最後の砦であり、有効なまま運用するには PC 側の定期再送が要る。

    安全側の挙動:
      - 緊急停止中は 1 通も送らない (再送は停止指令を上書きしてしまう)
      - 目標が一度も設定されていないモータへは送らない (起動直後の暴発防止)
      - ``pause()`` 中は送らない (動作確認が同じモータへ自前の指令を出すため)
      - 1 台の送信失敗で他のモータの再送を諦めない
      - 周期処理で例外が出てもループは継続する (再送が止まると 500ms で機体が死ぬ)
    """

    def __init__(
        self,
        handles: Sequence[MotorHandle],
        *,
        interval_s: float = DEFAULT_INTERVAL_S,
        is_estop_active: EStopChecker | None = None,
        sleep: SleepFunc = asyncio.sleep,
    ) -> None:
        """
        Args:
            handles: 再送対象のモータハンドル (generic ドライバのモータ)
            interval_s: 再送周期 [s]
            is_estop_active: 緊急停止判定 (server.py の状態を後から注入する)
            sleep: 周期待ちに使う関数 (テストで差し替え可能)
        """
        self._handles = tuple(handles)
        self._interval_s = interval_s
        self._is_estop_active = is_estop_active
        self._sleep = sleep

        self._paused = False
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._log_at: dict[str, float] = {}
        # pause() が「送信中の 1 周期」を待ち合わせるための排他
        self._step_lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    #  状態
    # ------------------------------------------------------------------ #

    @property
    def motor_names(self) -> tuple[str, ...]:
        return tuple(handle.name for handle in self._handles)

    @property
    def is_paused(self) -> bool:
        return self._paused

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def set_sleep(self, sleep: SleepFunc) -> None:
        """周期待ち関数を差し替える (テスト用)。"""
        self._sleep = sleep

    # ------------------------------------------------------------------ #
    #  再送
    # ------------------------------------------------------------------ #

    async def step(self) -> None:
        """1 周期分の再送を行う。run() から呼ばれるほか、テストから直接駆動できる。"""
        async with self._step_lock:
            await self._step_locked()

    async def _step_locked(self) -> None:
        if self._paused:
            return
        if self._is_estop_active is not None and self._is_estop_active():
            # 再送は最後の目標値をそのまま出すため、停止指令を上書きしてしまう
            return

        for handle in self._handles:
            try:
                await handle.resend_target()
            except asyncio.CancelledError:
                raise
            except Exception:
                # 1 台の失敗で残りのモータまで 500ms 後に止めてはならない
                self._log_throttled(
                    f"send:{handle.name}",
                    f"目標値の再送に失敗 (motor={handle.name})",
                )

    def clear_targets(self) -> None:
        """保持している目標を捨てる (緊急停止時に呼ぶ)。

        目標が残っていると、緊急停止を解除した瞬間に再送が走り、操縦者が
        何も操作していないのにコンベアが回り出す。停止操作そのものが次の
        駆動指令にならないよう、停止の時点で目標ごと落とす。
        """
        for handle in self._handles:
            handle.clear_target()

    async def pause(self, *, reason: str = "") -> None:
        """再送を止める。戻り値時点で在庫の周期も送信済みでないことを保証する。

        アクチュエータ動作確認は同じモータへ自前の指令を出す。古い目標値を
        20Hz で被せると動作確認の指令が打ち消され、健全なモータが FAILED になる。
        """
        async with self._step_lock:
            if self._paused:
                return
            self._paused = True
            logger.info(
                "目標値再送を一時停止 (%s%s)",
                ", ".join(self.motor_names),
                f", 理由={reason}" if reason else "",
            )

    def resume(self) -> None:
        """一時停止を解除する。

        同期メソッドにしてあるのは、動作確認側の ``finally`` から待ち合わせなしで
        必ず呼べるようにするため。復帰に失敗すると以後コンベアが動かなくなる。
        """
        if not self._paused:
            return
        self._paused = False
        logger.info("目標値再送を再開 (%s)", ", ".join(self.motor_names))

    # ------------------------------------------------------------------ #
    #  ライフサイクル
    # ------------------------------------------------------------------ #

    async def run(self) -> None:
        """停止要求まで再送を回し続ける。

        終了時に停止指令は送らない。指令が途切れればファーム側のウォッチドッグが
        500ms 以内に出力を止めるため、PC が落ちる場合も含めてそちらに委ねる方が
        経路が 1 本で済む。
        """
        while not self._stop_event.is_set():
            try:
                await self.step()
            except asyncio.CancelledError:
                raise
            except Exception:
                # 再送が止まると 500ms 後に機体が丸ごと止まる。周期は維持する
                self._log_throttled("step", "目標値再送の周期処理で例外")
            await self._sleep(self._interval_s)

    def start(self) -> None:
        """run() をバックグラウンドタスクとして起動する。"""
        if self.is_running:
            raise RuntimeError("目標値再送タスクは既に実行中です")
        self._stop_event.clear()
        self._task = asyncio.create_task(self.run())

    def request_stop(self) -> None:
        """次の周期でループを抜けるよう要求する (同期)。"""
        self._stop_event.set()

    async def stop(self) -> None:
        """再送を止めてタスクの終了を待つ。"""
        self.request_stop()
        task = self._task
        self._task = None
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def _log_throttled(self, key: str, message: str) -> None:
        now = asyncio.get_running_loop().time()
        last = self._log_at.get(key)
        if last is not None and now - last < _LOG_THROTTLE_S:
            return
        self._log_at[key] = now
        logger.exception(message)
