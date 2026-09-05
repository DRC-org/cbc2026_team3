from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING

from lib.control.periodic import PausablePeriodicTask
from lib.drivers.dm3520 import Dm3520Driver
from lib.drivers.edulite05 import Edulite05Driver
from lib.sequence.motors import MotorHandle

if TYPE_CHECKING:
    from lib.can_manager import CANManager

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_INTERVAL_S",
    "FIRMWARE_COMMAND_TIMEOUT_S",
    "GenericTargetRefresher",
    "QueryDrivenTargetRefresher",
    "TargetRefresher",
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

#: フィードバックが問い合わせ駆動のドライバ。**PC が黙ると 1 通も届かない**ので、
#: 目標を持たない間も送り続けないと操縦していない時間がまるごと STALE になる。
#: ここへ足す条件は「自分宛のフレームを受けたときにしか状態を返さない」ことと
#: ``idle_target_value()`` を持つこと (無害な指令値を自分で決められること) の 2 つ。
_QUERY_DRIVEN_DRIVERS = (Dm3520Driver, Edulite05Driver)


class _TargetRefresherBase(PausablePeriodicTask):
    """目標値を周期送信する 2 タスクの共通部。

    **差は 3 つだけで、そこは各サブクラスに残す**:
      1. 緊急停止中に送るか (自作モタドラは送らない / 問い合わせ駆動は送る)
      2. 目標を持たないモータへ何を書くか (前者は何も送らない / 後者はラッチ値)
      3. ``can_manager`` を直接持つか (後者だけが「目標として記録されない送信」を要る)

    骨格 (対象ハンドルの保持・名前の列挙・目標の破棄・降り際の扱い) は同じで、
    書き写すと片方だけ直した状態が作れる。共通の約束は 2 つ:
      - 1 台の送信失敗で他のモータの送信を諦めない
      - 周期処理で例外が出てもループは継続する
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
            handles: 送信対象のモータハンドル
            interval_s: 送信周期 [s]
            is_estop_active: 緊急停止判定 (server.py の状態を後から注入する)
            time_source: 周期とログ間引きに使う単調クロック
            sleep: 周期待ちに使う関数 (テストで差し替え可能)
        """
        super().__init__(interval_s=interval_s, time_source=time_source, sleep=sleep, logger=logger)
        self._handles = tuple(handles)
        self._is_estop_active = is_estop_active

    @property
    def motor_names(self) -> tuple[str, ...]:
        return tuple(handle.name for handle in self._handles)

    def clear_targets(self) -> None:
        """保持している目標を捨てる (緊急停止時に呼ぶ)。

        目標が残っていると、緊急停止を解除した瞬間に再送が走り、操縦者が
        何も操作していないのにコンベアが回り出す。停止操作そのものが次の
        駆動指令にならないよう、停止の時点で目標ごと落とす。

        **副作用として、その瞬間に ``move_to`` の到達待ちに入っている
        ``MotorHandle.wait_reached`` があれば中断させる。** 待機開始時点で
        目標を持っていたハンドルは ``WaitInterruptedError`` を送出するので、
        中断された動作は「到達した」として扱われない
        (詳細は ``lib.sequence.motors.WaitInterruptedError``)。
        """
        for handle in self._handles:
            handle.clear_target()

    async def _on_run_exit(self) -> None:
        """**降り際に停止指令も無励磁化も送らない。** 理由は 2 タスクで別々にある。

        自作モタドラ: 指令が途切れればファーム側のウォッチドッグが 500ms 以内に
        出力を止める。PC が落ちる場合も含めてそちらに委ねる方が経路が 1 本で済む。

        問い合わせ駆動 (DM3520 / EDULITE 05): 最後に受けた目標を内部の位置ループで
        保持し続ける。``main()`` の後始末が走る場面 —— systemd の停止や Ctrl-C ——
        で機構が保持を失って自重で落ちる方が危険なので、無励磁化しない。意図した
        停止は緊急停止 (``emergency_stop_message``) が担う。
        """


class GenericTargetRefresher(_TargetRefresherBase):
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


class QueryDrivenTargetRefresher(_TargetRefresherBase):
    """**問い合わせ駆動のドライバ**へ 20Hz で目標値を送り続ける非同期タスク。

    対象は DM3520 と EDULITE 05 の 2 種 (``_QUERY_DRIVEN_DRIVERS``)。どちらも
    「自分宛のフレームを受けたときにしか状態を返さない」性質を共有するので、
    タスクを分けない —— 分けると片方だけラッチの規則を直した状態が作れてしまい、
    しかも症状 (クリープ / 解除後の飛び) が出るのは直し忘れた側だけになる。

    ``GenericTargetRefresher`` と形は同じだが、**送る理由が 1 つ多い**。

    1. **フィードバックが問い合わせ駆動である。** 自分の CAN ID 宛のフレームを
       受けたときにしか状態を返さない (DM3520 は DM-S3519-1EC マニュアル
       「Control Protocol Description」節。EDULITE 05 は実機で確認 —— 励磁した
       まま 13 秒放置してフィードバックは 0 通、届くのは送ったフレームへの応答
       だけだった)。PC が黙ると 1 通も届かなくなり、``feedback_timeout_ms``
       (既定 500ms) を過ぎた時点でモータは ``MotorHealth.STALE`` になる。症状は
       「手動操縦すると動くのに常に赤い」だけで、配線不良と区別が付かない
    2. DM3520 の TIMEOUT レジスタ (0x09) が有効な個体では、指令の途絶がそのまま
       励磁解除になる (マニュアル「Characteristic Parameters」の Communication
       loss protection)。自作モタドラのウォッチドッグと同じ扱いが要る

    **目標をまだ持たないモータへも送る。** ここが ``GenericTargetRefresher`` との
    決定的な違いで、あちらは「目標が無い = 送らない」で正しい (送れば起動直後に
    コンベアが回り出す) が、本機で送らないことを選ぶと上の 1. により
    「励磁して待機しているだけの状態」が丸ごと観測できなくなる。

    送る中身は **``idle_target_value()`` をラッチした値**で、位置モードなら
    「目標を持たなくなった瞬間の実測角」。これは指令として無害である ——
    無励磁なら何も起きず、励磁中なら既に保っている位置を書き直すだけになる。
    **毎周期そのとき の実測角を書き直してはならない。** 負荷で下がったぶんへ
    目標が追従していき、誰も操作していないのに軸がじりじり動く (クリープする)。

    緊急停止中も送り続ける。停止中だけ画面から機体の状態が消えるのを避けるためで、
    上記のとおりこのフレームは機構を動かせない (緊急停止の解除は CAN の enable
    でしか起きず、このタスクは enable を 1 通も送らない)。ただし停止中はラッチを
    取らず毎回測り直す —— 停止直後の惰走中にラッチすると、解除後 1 周期目に
    その位置へ戻す動きが出る。
    """

    def __init__(
        self,
        handles: Sequence[MotorHandle],
        can_manager: CANManager,
        *,
        interval_s: float = DEFAULT_INTERVAL_S,
        is_estop_active: EStopChecker | None = None,
        time_source: Callable[[], float] = time.monotonic,
        sleep: SleepFunc = asyncio.sleep,
    ) -> None:
        """
        Args:
            handles: 再送対象のモータハンドル (dm3520 ドライバのモータ)
            can_manager: 目標を持たないモータへ問い合わせを送る経路。``MotorHandle``
                は「目標として記録される送信」しか持たないため直接渡す
                (記録させると ``wait_reached`` が誰も指令していない目標を待ち始める)
            interval_s: 送信周期 [s]
            is_estop_active: 緊急停止判定 (server.py の状態を後から注入する)
            time_source: 周期とログ間引きに使う単調クロック
            sleep: 周期待ちに使う関数 (テストで差し替え可能)
        """
        super().__init__(
            handles,
            interval_s=interval_s,
            is_estop_active=is_estop_active,
            time_source=time_source,
            sleep=sleep,
        )
        self._can_manager = can_manager
        # 目標を持たない間に書き続ける値。モータ名 -> ラッチ済みの指令値
        self._idle_targets: dict[str, float] = {}

    def _label(self) -> str:
        return f"問い合わせ駆動 目標値再送 ({', '.join(self.motor_names) or '対象なし'})"

    # ------------------------------------------------------------------ #
    #  再送
    # ------------------------------------------------------------------ #

    async def _step_locked(self) -> None:
        """1 周期分の送信。``step()`` (基底) から ``_step_lock`` 保持で呼ばれる。"""
        if self._paused:
            return

        for handle in self._handles:
            try:
                if await handle.resend_target():
                    # 操縦者・シーケンスが出した目標が生きている間はそれが正。
                    # 次に目標を失ったときは、その時点の姿勢でラッチを取り直す
                    self._idle_targets.pop(handle.name, None)
                    continue
                await self._send_idle_target(handle)
            except asyncio.CancelledError:
                raise
            except Exception:
                # 1 台の失敗で残りのモータのフィードバックまで止めてはならない
                self._log.exception(
                    f"send:{handle.name}",
                    "DM3520 への目標値送信に失敗 (motor=%s)",
                    handle.name,
                )

    async def _send_idle_target(self, handle: MotorHandle) -> None:
        """目標を持たないモータへ「今の姿勢を保て」を書き直す。"""
        driver = handle.driver
        if not isinstance(driver, _QUERY_DRIVEN_DRIVERS):
            return

        if self._is_estop_active is not None and self._is_estop_active():
            # 停止中はラッチしない (惰走中の値を掴むと解除後に戻る動きが出る)。
            # 無励磁なので、毎回測り直してもクリープは起こり得ない
            value = driver.idle_target_value()
            self._idle_targets.pop(handle.name, None)
        else:
            value = self._idle_targets.setdefault(handle.name, driver.idle_target_value())

        await self._can_manager.send(handle.name, driver.encode_target(driver.mode, value))

    def clear_targets(self) -> None:
        """保持している目標に加えてラッチも捨てる (緊急停止時に呼ぶ)。

        停止前の目標を保持位置として書き直し続けると、解除して励磁した瞬間に
        そこへ戻る動きになる。
        """
        super().clear_targets()
        self._idle_targets.clear()


#: 目標値再送タスクの共通型。``RobotServer`` は動作確認との排他 (pause/resume)、
#: 緊急停止時の ``clear_targets()``、診断表示にしかこれらを使わないため、
#: 種別を区別せず 1 つのリストで扱う。
TargetRefresher = GenericTargetRefresher | QueryDrivenTargetRefresher
