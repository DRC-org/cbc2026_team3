"""リミットスイッチまで軸を寄せて零点を確定する。"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping
from contextlib import AbstractContextManager

from lib.drivers.base import ControlMode
from lib.sequence.motors import AxisHandle
from lib.sequence.positions import AxisSpec, HomingSpec

logger = logging.getLogger(__name__)

__all__ = ["HomingError", "HomingRunner"]

_FOLLOW_ATTEMPTS = 5

_STALL_LIMIT = 3

_PROGRESS_FRACTION = 0.5

_RELEASE_STEP_LIMIT = 20


class HomingError(RuntimeError):
    """零点を確定できなかった。"""


SensorActive = Callable[[str], bool]
#: センサ名 → 立ち上がり (OFF→ON) を数えた接触回数。読んでも減らないので、
#: 零点確定以外の読み手 (リミットスイッチ保護) が同じセンサを見ても取りこぼさない
SensorContactCount = Callable[[str], int]
SensorStale = Callable[[str], bool]
MotorStale = Callable[[str], bool]
OriginCapturable = Callable[[str], bool]
CaptureOrigin = Callable[[str], Awaitable[None]]
SleepFunc = Callable[[float], Awaitable[None]]
#: センサ名の並び → そのセンサのリミット保護だけを外す contextmanager
#: (`LimitGuard.suspend_sensors`)。**軸ではなくセンサ単位**なのは、両端に
#: スイッチのある軸で反対端の保護まで消さないため。
SuspendSensors = Callable[[Iterable[str]], AbstractContextManager[None]]


@contextlib.contextmanager
def _no_suspend(_names: Iterable[str]) -> Iterator[None]:
    # 保護を持たない構成用の既定 (CAN も LimitGuard も無い状態で検証できる性質を保つ)。
    # 本番からは必ず本物を渡すこと —— 渡し忘れるとリミット保護が探索の 1 歩目を
    # 引き戻して零点確定が必ず失敗する
    yield


class _SensorContacts:
    # 基準値からの接触回数の増加を走行中センサごとに溜め込む。溜め込むのは、
    # どちらのスイッチが先に押されたかを整列段まで持ち越す必要があるため
    # (探索は「いずれか 1 本」で止まるが、整列段は「まだ押されていないのはどれか」を
    # 知らなければ指令先を選べない)

    def __init__(self, sensors: tuple[str, ...], count: SensorContactCount) -> None:
        self._sensors = sensors
        self._count = count
        #: センサ名 → 基準値。**初めて見た時点で取る** (それ以前の接触は見ない)。
        #: 探索の直前に `reset()` が取り直す
        self._baseline: dict[str, int] = {}
        self._latched: dict[str, bool] = dict.fromkeys(sensors, False)

    def poll(self) -> None:
        for name in self._sensors:
            count = self._count(name)
            if count != self._baseline.setdefault(name, count):
                self._latched[name] = True

    def reset(self) -> None:
        # 基準値を今の回数へ取り直す。探索を始める直前に 1 度だけ呼ぶ ——
        # 離脱段や前回の零点確定・手動操縦で跨いだぶんまで数えていると、
        # 1 歩目の観測でいきなり到達と読み、探索開始位置が原点になる
        for name in self._sensors:
            self._baseline[name] = self._count(name)
            self._latched[name] = False

    def any_latched(self) -> bool:
        self.poll()
        return any(self._latched.values())

    def latched(self, sensor: str) -> bool:
        return self._latched[sensor]


def _sensor_label(homing: HomingSpec) -> str:
    return " / ".join(f"'{name}'" for name in homing.sensor_names)


def _progress_threshold(homing: HomingSpec) -> float:
    return homing.step * _PROGRESS_FRACTION


class HomingRunner:
    def __init__(
        self,
        *,
        sensor_active: SensorActive,
        sensor_contact_count: SensorContactCount,
        sensor_is_stale: SensorStale,
        motor_is_stale: MotorStale,
        origin_capturable: OriginCapturable,
        capture_origin: CaptureOrigin,
        suspend_sensors: SuspendSensors = _no_suspend,
        sleep: SleepFunc = asyncio.sleep,
    ) -> None:
        self._sensor_active = sensor_active
        self._sensor_contact_count = sensor_contact_count
        self._sensor_is_stale = sensor_is_stale
        self._motor_is_stale = motor_is_stale
        self._origin_capturable = origin_capturable
        self._capture_origin = capture_origin
        self._suspend_sensors = suspend_sensors
        self._sleep = sleep

    async def home(self, spec: AxisSpec, handle: AxisHandle) -> float:
        homing = spec.homing
        if homing is None:
            raise HomingError(f"軸 '{spec.name}' に homing 設定がありません")

        self._check_preconditions(spec, homing)
        # **見るセンサ全部の保護を、事前確認の後から原点確定まで外す。**
        # 1 本でも外し忘れると、整列段で「まだ押されていない側」を進めている最中に
        # **既に押されている側**のセンサで保護が発動し、零点確定が必ず失敗する。
        # 場合分けを書かず `sensor_names` を使うのはそのため (単数形・複数形の
        # 違いを畳む唯一の口)。事前確認より前に外さないのは、外さなくても
        # 1 歩も動かない段だからである (保護は指令にしか効かない)。
        with self._suspend_sensors(homing.sensor_names):
            return await self._run_homing(spec, handle, homing)

    async def _run_homing(self, spec: AxisSpec, handle: AxisHandle, homing: HomingSpec) -> float:
        """離脱 → 探索 → 整列 → 原点確定。**リミット保護を外した状態で走る。**"""
        latches = _SensorContacts(homing.sensor_names, self._sensor_contact_count)

        if any(self._sensor_active(name) for name in homing.sensor_names):
            logger.info("[homing] %s: 既にセンサに触れているため一度離れて寄せ直す", spec.name)
            await self._seek(
                spec,
                handle,
                homing,
                latches,
                direction=-homing.direction,
                want_active=False,
                limit=homing.step * _RELEASE_STEP_LIMIT,
                limit_message=(
                    f"軸 '{spec.name}' を原点センサ {_sensor_label(homing)} から"
                    "離せませんでした"
                    f" ({homing.step * _RELEASE_STEP_LIMIT}{spec.unit} 動かしても OFF に"
                    " ならない)。**センサの極性が逆だとどこへ動かしても ON のまま**に"
                    " なるので、ファーム側の極性設定 (sensorActiveLow) を"
                    "接点の固着・配線の短絡と併せて確認してください"
                ),
            )

        origin = self._observe(spec, handle)
        observed = await self._seek(
            spec,
            handle,
            homing,
            latches,
            direction=homing.direction,
            want_active=True,
            limit=homing.search_distance,
            limit_message=(
                f"軸 '{spec.name}' が {homing.search_distance}{spec.unit} 動かしても"
                f" 原点センサ {_sensor_label(homing)} に到達しませんでした"
                " (探索方向・機構の引っかかり・センサの配線を確認してください)"
            ),
        )

        travelled = abs(observed - origin)
        logger.info("[homing] %s: %.2f%s 動かして原点に到達", spec.name, travelled, spec.unit)
        if homing.sensors is not None:
            await self._align(spec, handle, homing, homing.sensors, latches)
        await self._capture_origin(spec.name)
        return travelled

    async def _align(
        self,
        spec: AxisSpec,
        handle: AxisHandle,
        homing: HomingSpec,
        sensors: Mapping[str, str],
        latches: _SensorContacts,
    ) -> None:
        pending = [motor for motor, sensor in sensors.items() if not latches.latched(sensor)]
        if not pending:
            logger.info("[homing] %s: 整列段は不要 (全センサが同時に接触)", spec.name)
            self._log_sensor_states(spec, homing)
            return

        logger.info(
            "[homing] %s: 整列段 (未接触: %s)",
            spec.name,
            ", ".join(f"{motor}→{sensors[motor]}" for motor in pending),
        )

        hold = self._observe_each(spec, handle)
        start = dict(hold)
        stalled: dict[str, int] = dict.fromkeys(pending, 0)
        progress = _progress_threshold(homing)

        while True:
            latches.poll()
            observed = self._observe_each(spec, handle)
            for motor in [motor for motor in pending if latches.latched(sensors[motor])]:
                hold[motor] = observed[motor]
                pending.remove(motor)
                logger.info(
                    "[homing] %s: %s を %.2f%s 進めて %s に到達",
                    spec.name,
                    motor,
                    abs(observed[motor] - start[motor]),
                    spec.unit,
                    sensors[motor],
                )

            if not pending:
                await self._command_align(spec, handle, homing, hold, pending, observed)
                self._log_sensor_states(spec, homing)
                return

            self._check_align_distance(spec, homing, sensors, pending, observed, start)

            commanded = await self._command_align(spec, handle, homing, hold, pending, observed)
            await self._wait_align_step(spec, handle, homing, latches, sensors, pending, commanded)

            moved = self._observe_each(spec, handle)
            for motor in pending:
                stalled[motor] = (
                    0 if abs(moved[motor] - observed[motor]) >= progress else stalled[motor] + 1
                )
                if stalled[motor] >= _STALL_LIMIT:
                    raise HomingError(
                        f"軸 '{spec.name}' の整列段でモータ '{motor}' が指令しても動きません"
                        f" ({_STALL_LIMIT} 歩連続で {progress}{spec.unit} 進まなかった)。"
                        "**機構に遊びが無いと片側だけを動かせず、相方を引きずったまま"
                        "止まって見えます** —— 機構の引っかかり・モータの励磁と"
                        "併せて確認してください"
                    )

    async def _command_align(
        self,
        spec: AxisSpec,
        handle: AxisHandle,
        homing: HomingSpec,
        hold: Mapping[str, float],
        pending: list[str],
        observed: Mapping[str, float],
    ) -> dict[str, float]:
        values = {
            motor: (
                observed[motor] + homing.direction * homing.step
                if motor in pending
                else hold[motor]
            )
            for motor in spec.motor_names
        }
        await handle.set_target_value(spec.to_commands_each(values))
        return values

    def _check_align_distance(
        self,
        spec: AxisSpec,
        homing: HomingSpec,
        sensors: Mapping[str, str],
        pending: list[str],
        observed: Mapping[str, float],
        start: Mapping[str, float],
    ) -> None:
        limit = homing.align_distance
        if limit is None:
            return
        for motor in pending:
            if abs(observed[motor] - start[motor]) < limit:
                continue
            raise HomingError(
                f"軸 '{spec.name}' の整列段でモータ '{motor}' を {limit}{spec.unit}"
                f" 動かしても原点センサ '{sensors[motor]}' が反応しませんでした。"
                "**センサの極性が逆だと押しても ON になりません** —— ファーム側の"
                "極性設定 (sensorActiveLow) と、スイッチの配線・断線を確認してください"
            )

    async def _wait_align_step(
        self,
        spec: AxisSpec,
        handle: AxisHandle,
        homing: HomingSpec,
        latches: _SensorContacts,
        sensors: Mapping[str, str],
        pending: list[str],
        commanded: Mapping[str, float],
    ) -> None:
        reached = _progress_threshold(homing)
        for _ in range(_FOLLOW_ATTEMPTS):
            await self._sleep(homing.settle_s)
            latches.poll()
            if any(latches.latched(sensors[motor]) for motor in pending):
                return
            observed = self._observe_each(spec, handle)
            if all(abs(observed[motor] - commanded[motor]) <= reached for motor in pending):
                return

    def _log_sensor_states(self, spec: AxisSpec, homing: HomingSpec) -> None:
        logger.info(
            "[homing] %s: 原点確定 (センサ現在値: %s)",
            spec.name,
            ", ".join(
                f"{name}={'ON' if self._sensor_active(name) else 'OFF'}"
                for name in homing.sensor_names
            ),
        )

    async def _seek(
        self,
        spec: AxisSpec,
        handle: AxisHandle,
        homing: HomingSpec,
        latches: _SensorContacts,
        *,
        direction: int,
        want_active: bool,
        limit: float,
        limit_message: str,
    ) -> float:
        if want_active:
            # 探索を始める直前に基準値を取り直す。離脱段のあいだに触れたぶんや、
            # 前回の零点確定・手動操縦でスイッチを跨いだぶんまで数えていると、
            # 1 歩目の観測でいきなり到達と読み、探索開始位置が原点になる
            latches.reset()

        start = self._observe(spec, handle)
        observed = start
        stalled = 0
        while True:
            if abs(observed - start) >= limit:
                raise HomingError(limit_message)

            commanded = observed + direction * homing.step
            await handle.set_target_value(spec.to_commands(commanded))

            hit = await self._wait_step(
                spec, handle, homing, latches, commanded, want_active=want_active
            )

            previous = observed
            observed = self._observe(spec, handle)

            if hit:
                await handle.set_target_value(spec.to_commands(observed))
                return observed

            progress = _progress_threshold(homing)
            stalled = 0 if abs(observed - previous) >= progress else stalled + 1
            if stalled >= _STALL_LIMIT:
                raise HomingError(
                    f"軸 '{spec.name}' が指令しても動きません"
                    f" ({_STALL_LIMIT} 歩連続で {progress}{spec.unit} 進まなかった)。"
                    " 機構の引っかかり・探索方向・モータの励磁を確認してください"
                )

    def _check_preconditions(self, spec: AxisSpec, homing: HomingSpec) -> None:
        if spec.command_mode is not ControlMode.POSITION:
            raise HomingError(
                f"軸 '{spec.name}' は位置指令ではないため零点確定できません"
                f" (command_mode={spec.command_mode.value})"
            )

        if not self._origin_capturable(spec.name):
            raise HomingError(
                f"軸 '{spec.name}' の原点を確定する手段がありません。"
                " 零点確定を実行できないため探索を開始しません"
            )

        stale_sensors = [name for name in homing.sensor_names if self._sensor_is_stale(name)]
        if stale_sensors:
            label = " / ".join(f"'{name}'" for name in stale_sensors)
            raise HomingError(
                f"軸 '{spec.name}' の原点センサ {label} が応答していません"
                " (配線・基板の電源を確認してください)"
            )

        stale = [name for name in spec.motor_names if self._motor_is_stale(name)]
        if stale:
            raise HomingError(
                f"軸 '{spec.name}' の現在位置を読めません"
                f" (モータ {', '.join(stale)} のフィードバックが途絶しています)"
            )

    async def _wait_step(
        self,
        spec: AxisSpec,
        handle: AxisHandle,
        homing: HomingSpec,
        latches: _SensorContacts,
        commanded: float,
        *,
        want_active: bool,
    ) -> bool:
        reached = _progress_threshold(homing)
        for _ in range(_FOLLOW_ATTEMPTS):
            await self._sleep(homing.settle_s)
            if self._sensor_reached(homing, latches, want_active=want_active):
                return True
            if abs(self._observe(spec, handle) - commanded) <= reached:
                return False
        return False

    def _sensor_reached(
        self, homing: HomingSpec, latches: _SensorContacts, *, want_active: bool
    ) -> bool:
        # 探索と離脱で見るものが違う。探索は接触回数の増加を見る —— ON 区間が step より
        # 狭いと指令 1 回で区間を跨ぎ、settle_s 後の観測ではもう OFF になっているため
        # (越えた先は機構の破損側)。離脱は現在値を見る —— 同じ数え方を持ち込むと接点の
        # チャタリングで OFF が 1 回混じっただけで離脱完了と読む。取りこぼしの向きも
        # 非対称で、離脱の取りこぼしは「余計に離れる」だけで次の探索が寄せ直す。
        # センサが複数あっても探索は「いずれか 1 本」、離脱は「全センサが OFF」まで
        if want_active:
            return latches.any_latched()
        return not any(self._sensor_active(name) for name in homing.sensor_names)

    def _observe(self, spec: AxisSpec, handle: AxisHandle) -> float:
        try:
            return handle.observed_value()
        except HomingError:
            raise
        except Exception as exc:
            raise HomingError(f"軸 '{spec.name}' の現在位置を読めません ({exc})") from exc

    def _observe_each(self, spec: AxisSpec, handle: AxisHandle) -> dict[str, float]:
        try:
            return handle.observed_values()
        except HomingError:
            raise
        except Exception as exc:
            raise HomingError(f"軸 '{spec.name}' の現在位置を読めません ({exc})") from exc
