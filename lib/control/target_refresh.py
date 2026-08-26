from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Sequence

from lib.control.periodic import PausablePeriodicTask
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

EStopChecker = Callable[[], bool]
SleepFunc = Callable[[float], Awaitable[None]]


class GenericTargetRefresher(PausablePeriodicTask):
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

    ライフサイクル (start / stop / pause / resume) は ``PausablePeriodicTask`` と共通。
    """

    def __init__(
        self,
        handles: Sequence[MotorHandle],
        *,
        interval_s: float = DEFAULT_INTERVAL_S,
        is_estop_active: EStopChecker | None = None,
        time_source: Callable[[], float] = time.monotonic,
        sleep: SleepFunc = asyncio.sleep,
    ) -> None:
        """
        Args:
            handles: 再送対象のモータハンドル (generic ドライバのモータ)
            interval_s: 再送周期 [s]
            is_estop_active: 緊急停止判定 (server.py の状態を後から注入する)
            time_source: 周期とログ間引きに使う単調クロック
            sleep: 周期待ちに使う関数 (テストで差し替え可能)
        """
        super().__init__(interval_s=interval_s, time_source=time_source, sleep=sleep, logger=logger)
        self._handles = tuple(handles)
        self._is_estop_active = is_estop_active

    # ------------------------------------------------------------------ #
    #  状態
    # ------------------------------------------------------------------ #

    @property
    def motor_names(self) -> tuple[str, ...]:
        return tuple(handle.name for handle in self._handles)

    def _label(self) -> str:
        return f"目標値再送 ({', '.join(self.motor_names) or '対象なし'})"

    # ------------------------------------------------------------------ #
    #  再送
    # ------------------------------------------------------------------ #

    async def _step_locked(self) -> None:
        """1 周期分の再送。``step()`` (基底) から ``_step_lock`` 保持で呼ばれる。"""
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
                self._log.exception(
                    f"send:{handle.name}",
                    "目標値の再送に失敗 (motor=%s)",
                    handle.name,
                )

    def clear_targets(self) -> None:
        """保持している目標を捨てる (緊急停止時に呼ぶ)。

        目標が残っていると、緊急停止を解除した瞬間に再送が走り、操縦者が
        何も操作していないのにコンベアが回り出す。停止操作そのものが次の
        駆動指令にならないよう、停止の時点で目標ごと落とす。
        """
        for handle in self._handles:
            handle.clear_target()

    # ------------------------------------------------------------------ #
    #  ライフサイクル
    # ------------------------------------------------------------------ #

    async def _on_run_exit(self) -> None:
        """終了時に停止指令は送らない。

        指令が途切れればファーム側のウォッチドッグが 500ms 以内に出力を止めるため、
        PC が落ちる場合も含めてそちらに委ねる方が経路が 1 本で済む。
        """
