from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from types import MappingProxyType
from typing import Any, Protocol

import can

from lib.config_schema import DEFAULT_HEALTH, HealthThresholds
from lib.control.periodic import LogThrottle
from lib.drivers.base import MotorDriver
from lib.drivers.generic import GenericDriver
from lib.health import (
    BusHealth,
    BusHealthInfo,
    HealthSnapshot,
    MotorHealth,
    MotorHealthInfo,
)

logger = logging.getLogger(__name__)

_RECV_TIMEOUT = 0.01

# python-can の recv は timeout=0 を「select を 0 秒で打ち切る」と解釈する
# (None は「今は 1 通も無い」であって失敗ではない)。
_RECV_NO_WAIT = 0.0

_RX_BATCH_MAX = 64

# SocketCAN エラーフレーム (linux/can/error.h)。python-can の socketcan バスは既定で
# 受信を有効にし (CAN_RAW_ERR_FILTER = 0x1FFFFFFF)、種別は arbitration_id に載る。
_CAN_ERR_BUSOFF = 0x00000040
_CAN_ERR_RESTARTED = 0x00000100

_TX_ERROR_SCORE_FAIL = 8
_TX_ERROR_SCORE_MAX = 255

_RECV_RETRY_MIN_S = 0.02
_RECV_RETRY_MAX_S = 0.2


# fd の可読通知で起こして滞留を出し切る方式。エグゼキュータ往復の実測 168us/通に対し
# 47.5us/通で、バス 1 本ぶんの CPU が 37.5% → 10.6%。
class _ReadableFd:
    def __init__(self, fd: int) -> None:
        self._loop = asyncio.get_running_loop()
        self._fd = fd
        self._ready = asyncio.Event()
        self._armed = False
        self.resume()

    @classmethod
    def for_bus(cls, bus: can.Bus) -> _ReadableFd | None:
        fileno = getattr(bus, "fileno", None)
        if not callable(fileno):
            return None
        try:
            fd = fileno()
        except Exception:
            return None
        if not isinstance(fd, int) or fd < 0:
            return None
        try:
            return cls(fd)
        except (NotImplementedError, OSError, ValueError):
            return None

    async def wait(self) -> None:
        await self._ready.wait()
        self._ready.clear()

    def resume(self) -> None:
        if not self._armed:
            self._loop.add_reader(self._fd, self._ready.set)
            self._armed = True
        self._ready.set()

    def suspend(self) -> None:
        if self._armed:
            self._loop.remove_reader(self._fd)
            self._armed = False
        self._ready.clear()

    def close(self) -> None:
        self.suspend()


class BlockingRunner(Protocol):
    def __call__[T](self, func: Callable[..., T], /, *args: Any) -> Awaitable[T]: ...


async def _run_in_default_executor[T](func: Callable[..., T], /, *args: Any) -> T:
    return await asyncio.get_running_loop().run_in_executor(None, func, *args)


_ACTIVATION_FEEDBACK_TIMEOUT_S = 0.5
_ACTIVATION_PROBE_INTERVAL_S = 0.05


class CANManager:
    def __init__(self, *, run_blocking: BlockingRunner = _run_in_default_executor) -> None:
        self._run_blocking = run_blocking
        self._buses: dict[str, can.Bus] = {}
        self._motors: dict[str, MotorDriver] = {}
        self._sensors: dict[str, MotorDriver] = {}
        self._motor_bus: dict[str, str] = {}
        self._bus_motors: dict[str, list[MotorDriver]] = {}
        self._tasks: list[asyncio.Task[None]] = []

        self._last_rx_at: dict[str, float] = {}
        # **「新しく届いたか」の判定はこちら。** `_last_rx_at` は表示と経過時間の
        # ための壁時計なので、NTP が時刻を後ろへ補正すると「後に届いたフレームの
        # 記録のほうが小さい」が成立する。それで新規判定を行うと、届き続けている
        # のに `_wait_fresh_feedback` がタイムアウトし、症状は「フィードバックを
        # 受信できないため有効化を見送りました」だけで配線不良と区別が付かない。
        # 単調増加のカウンタなら時計に依らない
        self._rx_seq: dict[str, int] = {}
        self._last_tx_at: dict[str, float] = {}
        self._tx_error_count: dict[str, int] = {}
        self._tx_error_score: dict[str, int] = {}
        self._rx_error_count: dict[str, int] = {}
        self._bus_off: dict[str, bool] = {}
        self._rx_down: dict[str, bool] = {}
        self._rx_down_since: dict[str, float] = {}
        self._rx_down_episodes: dict[str, int] = {}
        self._bus_channels: dict[str, str] = {}

        self._rx_log = LogThrottle(logger)

    def add_bus(self, name: str, bus: can.Bus, channel: str = "") -> None:
        self._buses[name] = bus
        self._bus_motors.setdefault(name, [])

        if not channel:
            channel = getattr(bus, "channel_info", "") or ""
        self._bus_channels[name] = channel

        self._tx_error_count.setdefault(name, 0)
        self._tx_error_score.setdefault(name, 0)
        self._rx_error_count.setdefault(name, 0)
        self._bus_off.setdefault(name, False)
        self._rx_down.setdefault(name, False)
        self._rx_down_episodes.setdefault(name, 0)

    def add_motor(self, bus_name: str, motor: MotorDriver) -> None:
        self._add_device(bus_name, motor)
        self._motors[motor.name] = motor

    def _add_device(self, bus_name: str, device: MotorDriver) -> None:
        if bus_name not in self._buses:
            raise KeyError(f"バス '{bus_name}' が登録されていません")

        existing_bus = self._motor_bus.get(device.name)
        if existing_bus is not None:
            raise ValueError(f"デバイス '{device.name}' は既に登録済み (bus={existing_bus})")

        for existing in self._bus_motors[bus_name]:
            if existing.can_id == device.can_id:
                raise ValueError(
                    f"バス '{bus_name}' の can_id 0x{device.can_id:02X} が重複 "
                    f"('{device.name}' と '{existing.name}')"
                )

        self._motor_bus[device.name] = bus_name
        self._bus_motors[bus_name].append(device)

    def add_sensor(self, bus_name: str, sensor: MotorDriver) -> None:
        self._add_device(bus_name, sensor)
        self._sensors[sensor.name] = sensor

    @property
    def sensors(self) -> Mapping[str, MotorDriver]:
        return MappingProxyType(self._sensors)

    @property
    def motors(self) -> Mapping[str, MotorDriver]:
        return MappingProxyType(self._motors)

    @property
    def bus_names(self) -> tuple[str, ...]:
        return tuple(self._buses)

    def last_feedback_at(self, motor_name: str) -> float | None:
        return self._last_rx_at.get(motor_name)

    async def send(self, motor_name: str, msg: can.Message) -> None:
        bus_name = self._motor_bus[motor_name]
        await self.send_to_bus(bus_name, msg)

    async def send_to_bus(self, bus_name: str, msg: can.Message) -> None:
        bus = self._buses[bus_name]
        try:
            await self._run_blocking(bus.send, msg)
        except can.CanError:
            self._record_tx_failure(bus_name)
            raise
        except Exception:
            self._record_tx_failure(bus_name)
            raise
        else:
            self._last_tx_at[bus_name] = time.time()
            self._record_tx_success(bus_name)

    def _record_tx_failure(self, bus_name: str) -> None:
        self._tx_error_count[bus_name] = self._tx_error_count.get(bus_name, 0) + 1
        self._tx_error_score[bus_name] = min(
            _TX_ERROR_SCORE_MAX, self._tx_error_score.get(bus_name, 0) + _TX_ERROR_SCORE_FAIL
        )

    def _record_tx_success(self, bus_name: str) -> None:
        self._tx_error_score[bus_name] = max(0, self._tx_error_score.get(bus_name, 0) - 1)
        self._bus_off[bus_name] = False

    async def _receive_loop(self, bus_name: str) -> None:
        bus = self._buses[bus_name]
        motors = self._bus_motors[bus_name]
        retry_s = _RECV_RETRY_MIN_S
        readable = _ReadableFd.for_bus(bus)

        try:
            while True:
                try:
                    msgs = await self._receive_batch(bus, readable)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._record_rx_down(bus_name)
                    self._rx_log.exception(
                        f"{bus_name}:recv",
                        "CAN 受信に失敗しました。再試行を続けます (bus=%s)",
                        bus_name,
                    )
                    if readable is not None:
                        readable.suspend()
                    await asyncio.sleep(retry_s)
                    retry_s = min(_RECV_RETRY_MAX_S, retry_s * 2)
                    if readable is not None:
                        readable.resume()
                    continue

                retry_s = _RECV_RETRY_MIN_S

                if not msgs:
                    continue

                self._clear_rx_down(bus_name)
                for msg in msgs:
                    if msg.is_error_frame:
                        self._handle_error_frame(bus_name, msg)
                        continue
                    self._dispatch_frame(bus_name, motors, msg)

        finally:
            if readable is not None:
                readable.close()

    async def _receive_batch(
        self, bus: can.Bus, readable: _ReadableFd | None
    ) -> Sequence[can.Message]:
        if readable is None:
            msg: can.Message | None = await self._run_blocking(bus.recv, _RECV_TIMEOUT)
            return () if msg is None else (msg,)

        await readable.wait()

        msgs: list[can.Message] = []
        while len(msgs) < _RX_BATCH_MAX:
            try:
                msg = bus.recv(_RECV_NO_WAIT)
            except asyncio.CancelledError:
                raise
            except Exception:
                if msgs:
                    return msgs
                raise
            if msg is None:
                break
            msgs.append(msg)
        return msgs

    def _record_rx_down(self, bus_name: str) -> None:
        if not self._rx_down.get(bus_name, False):
            self._rx_down_since[bus_name] = time.time()
            self._rx_down_episodes[bus_name] = self._rx_down_episodes.get(bus_name, 0) + 1
            logger.error("CAN 受信が中断しました。復帰まで再試行を続けます (bus=%s)", bus_name)
        self._rx_down[bus_name] = True

    def _clear_rx_down(self, bus_name: str) -> None:
        if self._rx_down.get(bus_name, False):
            since = self._rx_down_since.pop(bus_name, None)
            gap_s = time.time() - since if since is not None else 0.0
            logger.warning("CAN 受信が再開しました (bus=%s, 中断 %.2f 秒)", bus_name, gap_s)
        self._rx_down[bus_name] = False

    def reset_rx_down_episodes(self) -> None:
        for bus_name in self._rx_down_episodes:
            self._rx_down_episodes[bus_name] = 0

    def _dispatch_frame(
        self, bus_name: str, motors: Sequence[MotorDriver], msg: can.Message
    ) -> None:
        for motor in motors:
            try:
                claimed = motor.matches_feedback(msg)
            except asyncio.CancelledError:
                raise
            except Exception:
                self._record_rx_error(bus_name, motor.name, "宛先判定")
                continue

            if claimed:
                try:
                    motor.update_state(msg)
                    self._last_rx_at[motor.name] = time.time()
                    self._rx_seq[motor.name] = self._rx_seq.get(motor.name, 0) + 1
                    self._bus_off[bus_name] = False
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._record_rx_error(bus_name, motor.name, "状態更新")
                return

            try:
                info_claimed = motor.matches_info(msg)
            except asyncio.CancelledError:
                raise
            except Exception:
                self._record_rx_error(bus_name, motor.name, "INFO 宛先判定")
                continue

            if not info_claimed:
                continue

            try:
                motor.update_info(msg)
            except asyncio.CancelledError:
                raise
            except Exception:
                self._record_rx_error(bus_name, motor.name, "INFO 解釈")
            return

    def _handle_error_frame(self, bus_name: str, msg: can.Message) -> None:
        if msg.arbitration_id & _CAN_ERR_BUSOFF:
            if not self._bus_off.get(bus_name, False):
                logger.error("CAN バスが bus-off になりました (bus=%s)", bus_name)
            self._bus_off[bus_name] = True
        if msg.arbitration_id & _CAN_ERR_RESTARTED:
            logger.warning("CAN バスが bus-off から自動復帰しました (bus=%s)", bus_name)
            self._bus_off[bus_name] = False

    def _record_rx_error(self, bus_name: str, motor_name: str, phase: str) -> None:
        self._rx_error_count[bus_name] = self._rx_error_count.get(bus_name, 0) + 1
        self._rx_log.exception(
            f"{bus_name}:{motor_name}:{phase}",
            "CAN 受信フレームの%sで例外 (bus=%s, motor=%s)。このフレームは破棄します",
            phase,
            bus_name,
            motor_name,
        )

    async def run(self) -> list[str]:
        for bus_name in self._buses:
            task = asyncio.create_task(self._receive_loop(bus_name))
            self._tasks.append(task)
        return await self.initialize_motors()

    async def initialize_motors(self) -> list[str]:
        async def initialize_and_activate(motor_name: str) -> bool:
            await self._send_steps(motor_name, self._motors[motor_name].initialization_steps())
            return await self.activate_motor(motor_name)

        return await self._activate_each_motor("起動時設定", initialize_and_activate)

    async def activate_motors(
        self,
        *,
        should_abort: Callable[[], bool] | None = None,
        feedback_timeout_s: float = _ACTIVATION_FEEDBACK_TIMEOUT_S,
        only: Collection[str] | None = None,
    ) -> list[str]:
        async def activate(motor_name: str) -> bool:
            return await self.activate_motor(
                motor_name,
                should_abort=should_abort,
                feedback_timeout_s=feedback_timeout_s,
                # この経路はどれも電源断を跨ぎうる復帰で、跨いだかを判断できるのはドライバだけ。
                reinitialize=True,
            )

        return await self._activate_each_motor(
            "有効化", activate, should_abort=should_abort, only=only
        )

    async def clear_e_stop_latches(self) -> list[str]:
        async def clear(motor_name: str) -> bool:
            motor = self._motors[motor_name]
            if not isinstance(motor, GenericDriver):
                return True
            await self._send_steps(motor_name, motor.activation_steps())
            return True

        return await self._activate_each_motor("緊急停止ラッチの解除", clear)

    async def _activate_each_motor(
        self,
        what: str,
        action: Callable[[str], Awaitable[bool]],
        *,
        should_abort: Callable[[], bool] | None = None,
        only: Collection[str] | None = None,
    ) -> list[str]:
        inactive: list[str] = []
        motor_names = [name for name in self._motors if only is None or name in only]
        for index, motor_name in enumerate(motor_names):
            if should_abort is not None and should_abort():
                logger.warning("モータの有効化を中断しました (残り: %s 以降)", motor_name)
                inactive.extend(motor_names[index:])
                return inactive
            try:
                if not await action(motor_name):
                    inactive.append(motor_name)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("モータ '%s' の%sに失敗しました", motor_name, what)
                inactive.append(motor_name)
        return inactive

    async def capture_origin_via_set_zero(
        self, motor_names: Sequence[str], *, should_abort: Callable[[], bool] | None = None
    ) -> None:
        """指定したモータ群の原点を「今の位置」へまとめて切り直す (零点確定)。

        軸のモータ全員をまとめて受け取るのは、別々の時刻に確定すると消えない
        オフセットが残るため。順序 (無励磁 -> 付け替え -> 再励磁) は
        `initialization_steps()` が確立したものをそのまま使う。

        ``should_abort`` は**最後の再励磁だけ**を中断する。付け替えの窓 (約 0.5 秒)
        で緊急停止が入ると停止の disable の後に enable が届き、停止中に励磁が残る。
        無励磁化と付け替えは中断しない (中断してよいのは励磁だけ)。

        Raises:
            ValueError: 原点を切り直す手段を持たないモータが混ざっている
            RuntimeError: 再励磁できなかった。黙って戻ると「指令しても動かない」になる
        """
        names = list(motor_names)
        unsupported = [name for name in names if not self._motors[name].supports_origin_capture()]
        if unsupported:
            raise ValueError(f"モータ {', '.join(unsupported)} は CAN 経由で原点を切り直せません")

        for name in names:
            await self._send_steps(name, self._motors[name].deactivation_steps())
        for name in names:
            await self._send_steps(name, self._motors[name].origin_capture_steps())

        inactive = [
            name
            for name in names
            if not await self.activate_motor(name, after_set_zero=True, should_abort=should_abort)
        ]
        if inactive:
            raise RuntimeError(
                f"原点を切り直しましたがモータ {', '.join(inactive)} を再励磁できません"
                " (緊急停止が入ったか、フィードバックが届いていません。"
                "緊急停止の解除、または配線・電源を確認してください)"
            )

    async def activate_motor(
        self,
        motor_name: str,
        *,
        should_abort: Callable[[], bool] | None = None,
        feedback_timeout_s: float = _ACTIVATION_FEEDBACK_TIMEOUT_S,
        after_set_zero: bool = False,
        reinitialize: bool = False,
    ) -> bool:
        motor = self._motors[motor_name]

        # 後ろへ置くと、控えた値を捨てる前に読み直して電源断より前の値で確認済みにしてしまう。
        if reinitialize and not self._is_known_energized(motor):
            await self._send_steps(motor_name, motor.reinitialization_steps())

        # 「まだ読めていない」を「問題なし」へ倒さないための再試行。
        await self._confirm_configuration(motor_name, feedback_timeout_s)

        # **「待てば解ける」より先に「待っても解けない」を見る。** 構成が食い違って
        # いるモータを鮮度待ちへ入れると、原因が「通信が遅い」に見えてしまう
        blocked = motor.activation_block_reason()
        if blocked is not None:
            logger.error(
                "モータ '%s' を励磁しません (無励磁のまま): %s",
                motor_name,
                blocked,
            )
            return False

        if motor.requires_fresh_feedback_for_activation() and not await self._wait_fresh_feedback(
            motor_name,
            feedback_timeout_s,
            probe=self._may_probe_for_feedback(motor, after_set_zero=after_set_zero),
        ):
            logger.warning(
                "モータ '%s' のフィードバックを %.2fs 以内に受信できないため"
                "有効化を見送りました (無励磁のまま)",
                motor_name,
                feedback_timeout_s,
            )
            return False

        steps = motor.activation_steps(after_set_zero=after_set_zero)
        if not steps:
            return True
        if should_abort is not None and should_abort():
            logger.warning("モータ '%s' の有効化を中断しました", motor_name)
            return False

        await self._send_steps(motor_name, steps)
        return True

    async def _send_steps(self, motor_name: str, steps: list[tuple[can.Message, float]]) -> None:
        for message, delay_after_s in steps:
            await self.send(motor_name, message)
            if delay_after_s > 0:
                await asyncio.sleep(delay_after_s)

    @staticmethod
    def _may_probe_for_feedback(motor: MotorDriver, *, after_set_zero: bool) -> bool:
        if after_set_zero:
            return True
        return not CANManager._is_known_energized(motor)

    @staticmethod
    def _is_known_energized(motor: MotorDriver) -> bool:
        """励磁中だと**分かっている**か (`is_energized()` の三値を 1 箇所で読む)。

        `None` (申告を持たない・未受信) は False へ倒す。「分からない」を
        「励磁中」と読むと、無励磁のモータへ届くはずの手当てが黙って止まる。

        **`disable` を含む手順を送ってよいかの判断は、すべてここを通る** ——
        鮮度確認の問い合わせ (`_may_probe_for_feedback`) と再初期化
        (`activate_motor` の `reinitialize`) の 2 つで、どちらも「励磁中の相方の
        保持トルクをその場で失わせない」という同じ理由に立つ。三値の読み方を
        呼び出し側へ書き写すと、片方だけ `is not False` のような別の丸め方に
        なった状態が作れる。
        """
        return motor.is_energized() is True

    async def _confirm_configuration(self, motor_name: str, timeout_s: float) -> None:
        """励磁前に確認しなければならない設定を、読めるまで問い合わせ直す。

        `activation_block_reason()` が「未確認だから止める」と答えるドライバ
        (DM3520 の固定小数点レンジ) のための再試行。**取りこぼしを「問題なし」へ
        倒して解いてはならない** —— 応答が 1 通落ちるだけで、ゲートが守っている
        事故の経路 (レンジ違いで比例倍に読めた位置がそのまま保持目標へ書かれる)
        が丸ごと復活する。かといって 1 通で諦めれば「CAN が 1 通落ちただけで
        機体が動かせない」になるので、既定を緩めるのではなくここで送り直す。

        **判断は下さない。** ここは材料を集めるだけで、集まらなかったときに
        どうするかは `activation_block_reason()` が決める (`_wait_fresh_feedback`
        と `_may_probe_for_feedback` を分けてあるのと同じ理由 —— 待ち方と判断を
        同じ関数へ混ぜると、呼び出しを 1 つ足した人が判断を書き写すことになる)。

        締切は鮮度待ちと同じ予算にしてある。どちらも「応答が返らないモータの
        ぶんだけ起動が遅れる上限」で、性質も手当ても同じ (電源・配線) なので、
        別の数字を持たせると片方だけ延ばした構成が作れる。

        送るのは設定フレームだけで、機構は動かない
        (`configuration_probe_messages()` の制約)。食い違いを直す書き込みも
        そこへ混ざって出るが、**書けたと数えるのは読み返せたときだけ**なので、
        このループの終わり方は「読めた」のままである。確認すべき設定を持たない
        ドライバは 1 通も送らずに即戻る。
        """
        motor = self._motors[motor_name]
        deadline = time.monotonic() + timeout_s
        while True:
            probes = motor.configuration_probe_messages()
            if not probes:
                return
            if time.monotonic() >= deadline:
                logger.warning(
                    "モータ '%s' の設定を %.2fs 以内に読み返せませんでした",
                    motor_name,
                    timeout_s,
                )
                return
            for probe in probes:
                try:
                    await self.send(motor_name, probe)
                except Exception:
                    # 送れないバスでも、既に飛んだぶんの応答は届きうる。
                    logger.debug("モータ '%s' への設定読み返しの送信に失敗", motor_name)
            await asyncio.sleep(_ACTIVATION_PROBE_INTERVAL_S)

    async def _wait_fresh_feedback(
        self, motor_name: str, timeout_s: float, *, probe: bool = True
    ) -> bool:
        """待機開始より後に届いたフィードバックを待つ。

        判定を壁時計 (`_last_rx_at`) に乗せると、NTP が時刻を後ろへ補正しただけで
        届き続けているのに新規と認められず、配線不良と同じ症状でタイムアウトする。
        """
        baseline = self._rx_seq.get(motor_name, 0)
        probe_msg = self._motors[motor_name].feedback_probe_message() if probe else None
        deadline = time.monotonic() + timeout_s

        while True:
            if self._rx_seq.get(motor_name, 0) > baseline:
                return True
            if time.monotonic() >= deadline:
                return False
            if probe_msg is not None:
                try:
                    await self.send(motor_name, probe_msg)
                except Exception:
                    logger.debug("モータ '%s' への問い合わせ送信に失敗", motor_name)
            await asyncio.sleep(_ACTIVATION_PROBE_INTERVAL_S)

    async def shutdown(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("受信タスクは既に異常終了していました")
        self._tasks.clear()

        for bus_name, bus in self._buses.items():
            try:
                bus.shutdown()
            except Exception:
                logger.exception("バスの停止に失敗: bus=%s", bus_name)

    def health(self, *, thresholds: HealthThresholds = DEFAULT_HEALTH) -> HealthSnapshot:
        now = time.time()

        buses: list[BusHealthInfo] = []
        motors: list[MotorHealthInfo] = []

        bus_latest_rx: dict[str, float] = {}
        for motor_name, motor in {**self._motors, **self._sensors}.items():
            bus_name = self._motor_bus[motor_name]
            last_fb = self._last_rx_at.get(motor_name)
            age_ms = (now - last_fb) * 1000.0 if last_fb is not None else None

            stale = last_fb is None or (
                age_ms is not None and age_ms > thresholds.feedback_timeout_ms
            )
            detail = motor.health_detail()
            warning = (
                motor.has_thermal_warning(thresholds.temp_warning_c)
                or motor.has_overcurrent_warning()
                or detail is not None
            )

            if motor.is_fault() or motor.has_thermal_fault(thresholds.temp_critical_c):
                state = MotorHealth.FAULT
            elif stale:
                state = MotorHealth.STALE
            elif warning:
                state = MotorHealth.WARNING
            else:
                state = MotorHealth.OK

            motors.append(
                MotorHealthInfo(
                    name=motor_name,
                    bus=bus_name,
                    state=state,
                    last_feedback_at=last_fb,
                    feedback_age_ms=age_ms,
                    temperature=(motor.state.temperature if motor.telemetry.temperature else None),
                    detail=detail,
                )
            )

            if last_fb is not None:
                prev = bus_latest_rx.get(bus_name)
                if prev is None or last_fb > prev:
                    bus_latest_rx[bus_name] = last_fb

        for bus_name, bus in self._buses.items():
            tx_err = self._tx_error_count.get(bus_name, 0)
            tx_score = self._tx_error_score.get(bus_name, 0)
            rx_err = self._rx_error_count.get(bus_name, 0)
            bus_off = self._bus_off.get(bus_name, False)
            rx_down = self._rx_down.get(bus_name, False)
            rx_down_episodes = self._rx_down_episodes.get(bus_name, 0)
            may_affect_workpiece = any(
                motor.has_on_off_control() for motor in self._bus_motors.get(bus_name, [])
            )

            can_state = getattr(bus, "state", None)
            error_state = getattr(can.BusState, "ERROR", None)
            passive_state = getattr(can.BusState, "PASSIVE", None)
            is_error = can_state is not None and can_state == error_state
            is_passive = can_state is not None and can_state == passive_state

            if bus_off or is_error or rx_down:
                state = BusHealth.DOWN
            elif tx_score >= thresholds.tx_error_threshold or is_passive:
                state = BusHealth.DEGRADED
            else:
                state = BusHealth.OK

            buses.append(
                BusHealthInfo(
                    name=bus_name,
                    channel=self._bus_channels.get(bus_name, ""),
                    state=state,
                    last_tx_at=self._last_tx_at.get(bus_name),
                    last_rx_at=bus_latest_rx.get(bus_name),
                    tx_error_count=tx_err,
                    rx_error_count=rx_err,
                    bus_off=bus_off,
                    rx_down=rx_down,
                    rx_down_episodes=rx_down_episodes,
                    may_affect_workpiece=may_affect_workpiece,
                )
            )

        overall = HealthSnapshot.compute_overall(buses, motors)
        return HealthSnapshot(timestamp=now, overall=overall, buses=buses, motors=motors)
