from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

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
SensorLatched = Callable[[str], bool]
SensorStale = Callable[[str], bool]
MotorStale = Callable[[str], bool]
OriginCapturable = Callable[[str], bool]
CaptureOrigin = Callable[[str], Awaitable[None]]
SleepFunc = Callable[[float], Awaitable[None]]


def _progress_threshold(homing: HomingSpec) -> float:
    return homing.step * _PROGRESS_FRACTION


class HomingRunner:
    def __init__(
        self,
        *,
        sensor_active: SensorActive,
        sensor_latched: SensorLatched,
        sensor_is_stale: SensorStale,
        motor_is_stale: MotorStale,
        origin_capturable: OriginCapturable,
        capture_origin: CaptureOrigin,
        sleep: SleepFunc = asyncio.sleep,
    ) -> None:
        self._sensor_active = sensor_active
        self._sensor_latched = sensor_latched
        self._sensor_is_stale = sensor_is_stale
        self._motor_is_stale = motor_is_stale
        self._origin_capturable = origin_capturable
        self._capture_origin = capture_origin
        self._sleep = sleep

    async def home(self, spec: AxisSpec, handle: AxisHandle) -> float:
        homing = spec.homing
        if homing is None:
            raise HomingError(f"軸 '{spec.name}' に homing 設定がありません")

        self._check_preconditions(spec, homing)

        if self._sensor_active(homing.sensor):
            logger.info("[homing] %s: 既にセンサに触れているため一度離れて寄せ直す", spec.name)
            await self._seek(
                spec,
                handle,
                homing,
                direction=-homing.direction,
                want_active=False,
                limit=homing.step * _RELEASE_STEP_LIMIT,
                limit_message=(
                    f"軸 '{spec.name}' を原点センサ '{homing.sensor}' から離せませんでした"
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
            direction=homing.direction,
            want_active=True,
            limit=homing.search_distance,
            limit_message=(
                f"軸 '{spec.name}' が {homing.search_distance}{spec.unit} 動かしても"
                f" 原点センサ '{homing.sensor}' に到達しませんでした"
                " (探索方向・機構の引っかかり・センサの配線を確認してください)"
            ),
        )

        travelled = abs(observed - origin)
        logger.info("[homing] %s: %.2f%s 動かして原点に到達", spec.name, travelled, spec.unit)
        await self._capture_origin(spec.name)
        return travelled

    async def _seek(
        self,
        spec: AxisSpec,
        handle: AxisHandle,
        homing: HomingSpec,
        *,
        direction: int,
        want_active: bool,
        limit: float,
        limit_message: str,
    ) -> float:
        if want_active:
            self._sensor_latched(homing.sensor)

        start = self._observe(spec, handle)
        observed = start
        stalled = 0
        while True:
            if abs(observed - start) >= limit:
                raise HomingError(limit_message)

            commanded = observed + direction * homing.step
            await handle.set_target_value(spec.to_commands(commanded))

            hit = await self._wait_step(spec, handle, homing, commanded, want_active=want_active)

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

        if self._sensor_is_stale(homing.sensor):
            raise HomingError(
                f"軸 '{spec.name}' の原点センサ '{homing.sensor}' が応答していません"
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
        commanded: float,
        *,
        want_active: bool,
    ) -> bool:
        reached = _progress_threshold(homing)
        for _ in range(_FOLLOW_ATTEMPTS):
            await self._sleep(homing.settle_s)
            if self._sensor_reached(homing.sensor, want_active=want_active):
                return True
            if abs(self._observe(spec, handle) - commanded) <= reached:
                return False
        return False

    def _sensor_reached(self, sensor: str, *, want_active: bool) -> bool:
        if want_active:
            return self._sensor_latched(sensor)
        return self._sensor_active(sensor) is False

    def _observe(self, spec: AxisSpec, handle: AxisHandle) -> float:
        try:
            return handle.observed_value()
        except HomingError:
            raise
        except Exception as exc:
            raise HomingError(f"軸 '{spec.name}' の現在位置を読めません ({exc})") from exc
