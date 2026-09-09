from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Collection, Iterable, Mapping
from dataclasses import dataclass

from lib.drivers.base import ControlMode
from lib.match_state import Court
from lib.sequence.motors import AxisHandle, MotorGroup
from lib.sequence.positions import AxisSpec, HomingSpec, PositionTable

logger = logging.getLogger(__name__)

__all__ = ["AxisHomingResult", "HomingError", "HomingRunner", "homing_axis_names", "run_homing"]

_FOLLOW_ATTEMPTS = 5

_STALL_LIMIT = 3

_PROGRESS_FRACTION = 0.5

#: `homing.release_distance` を書かなかった軸の離脱上限を step から作る倍数。
#: `search_distance` を流用すると反対側の機構端まで走り抜ける
_RELEASE_STEP_LIMIT = 20


class HomingError(RuntimeError):
    """零点を確定できなかった。"""


#: 三値。`None` (読めていない) を `False` へ丸めると、途絶したセンサが「離脱できた」に化ける
SensorActive = Callable[[str], bool | None]
#: 前回読んでから一度でも接触したか。**読むと消える**ので読み手は `HomingRunner` 1 つに限る。
#: `None` = ラッチを提供しないドライバ
SensorLatched = Callable[[str], bool | None]
SensorStale = Callable[[str], bool]
MotorStale = Callable[[str], bool]
#: 三値。`None` = 励磁を報告しないドライバ (M3508) であって無励磁ではない
MotorEnergized = Callable[[str], bool | None]
OriginCapturable = Callable[[str], bool]
CaptureOrigin = Callable[[str], Awaitable[None]]
SleepFunc = Callable[[float], Awaitable[None]]


class _SensorLatches:
    def __init__(self, sensors: tuple[str, ...], read: SensorLatched) -> None:
        self._sensors = sensors
        self._read = read
        self._latched: dict[str, bool] = dict.fromkeys(sensors, False)

    def poll(self) -> None:
        for name in self._sensors:
            if self._read(name) is True:
                self._latched[name] = True

    def discard(self) -> tuple[str, ...]:
        """溜まったラッチを捨て、ラッチを提供しなかったセンサ名を返す。"""
        unsupported = []
        for name in self._sensors:
            if self._read(name) is None:
                unsupported.append(name)
            self._latched[name] = False
        return tuple(unsupported)

    def any_latched(self) -> bool:
        self.poll()
        return any(self._latched.values())

    def latched(self, sensor: str) -> bool:
        return self._latched[sensor]


def _label(names: Iterable[str]) -> str:
    return " / ".join(f"'{name}'" for name in names)


def _release_limit(homing: HomingSpec) -> float:
    # 本来は ON 区間の広さで決まる値。step の倍数を既定にしてあるのは既存の軸を
    # 変えないためだけで、精度のために step を詰める軸ほど明示が要る
    if homing.release_distance is not None:
        return homing.release_distance
    return homing.step * _RELEASE_STEP_LIMIT


def _remaining_distance(homing: HomingSpec, *, travelled: float, released: float) -> float:
    """この段の探索に許す移動量 [軸の unit]。

    `search_distance` が縛るのは「開始位置からどれだけ押し込むか」なので、探索で
    進んだぶんは消費するが**離脱で戻ったぶんは消費しない** (逆向きなので、寄せ直しが
    もう一度走っても到達しうる最も深い点は離脱前より深くならない)。
    上限の判定は指令の前にしかなく最後の 1 歩は最大 `step` ぶん超えうるので、
    次の段の残りが負にならないよう 0 で床を張る。
    """
    return max(0.0, homing.search_distance - travelled + released)


def _not_reached_message(spec: AxisSpec, homing: HomingSpec, limit: float) -> str:
    return (
        f"軸 '{spec.name}' が {limit}{spec.unit} 動かしても"
        f" 原点センサ {_label(homing.sensor_names)} に到達しませんでした"
        " (探索方向・機構の引っかかり・センサの配線を確認してください)"
    )


def _progress_threshold(step: float) -> float:
    """「1 歩ぶん進んだ」と見なす移動量。**追従待ちと停滞判定が同じ 1 つを見る。**

    受け取るのは `HomingSpec` ではなく**その段の刻み** —— 二段探索では粗い段と
    細い段で刻みが 1 桁違うので、粗い側の基準を細い段へ持ち込むと動いている機構を
    数歩で止める。
    """
    return step * _PROGRESS_FRACTION


class HomingRunner:
    """1 軸ぶんのホーミングを実行する。

    センサの読み取りと原点確定は注入で受ける (直接 `CANManager` を掴むと
    CAN を立てないと 1 行も検証できなくなる)。
    """

    def __init__(
        self,
        *,
        sensor_active: SensorActive,
        sensor_latched: SensorLatched,
        sensor_is_stale: SensorStale,
        motor_is_stale: MotorStale,
        motor_is_energized: MotorEnergized,
        origin_capturable: OriginCapturable,
        capture_origin: CaptureOrigin,
        sleep: SleepFunc = asyncio.sleep,
    ) -> None:
        self._sensor_active = sensor_active
        self._sensor_latched = sensor_latched
        self._sensor_is_stale = sensor_is_stale
        self._motor_is_stale = motor_is_stale
        self._motor_is_energized = motor_is_energized
        self._origin_capturable = origin_capturable
        self._capture_origin = capture_origin
        self._sleep = sleep

    async def home(self, spec: AxisSpec, handle: AxisHandle) -> float:
        """`spec.homing` に従って軸を寄せ、当たった位置を原点として確定する。

        Returns:
            **探索で**動いた距離 [軸の unit]。二段探索では 2 段の合計で、
            離脱のぶんは含めない。
        """
        homing = spec.homing
        if homing is None:
            raise HomingError(f"軸 '{spec.name}' に homing 設定がありません")

        self._check_preconditions(spec, homing)
        latches = _SensorLatches(homing.sensor_names, self._sensor_latched)

        # 離脱で戻ったぶんは探索距離を消費しない (理由は `_remaining_distance`)
        released = 0.0
        travelled = 0.0

        # ここが問うのは「**今**触れているか」。ラッチで問うと、前回の零点確定や
        # 手動操縦でスイッチを跨いだ痕跡だけで離脱段へ入る
        if any(self._sensor_active(name) is True for name in homing.sensor_names):
            logger.info("[homing] %s: 既にセンサに触れているため一度離れて寄せ直す", spec.name)
            released += await self._release(spec, handle, homing, latches)

        if homing.coarse_step is not None:
            start = self._observe(spec, handle)
            limit = _remaining_distance(homing, travelled=travelled, released=released)
            observed = await self._seek(
                spec,
                handle,
                homing,
                latches,
                step=homing.coarse_step,
                direction=homing.direction,
                want_active=True,
                limit=limit,
                limit_message=_not_reached_message(spec, homing, limit),
            )
            travelled += abs(observed - start)
            logger.info(
                "[homing] %s: 粗探索 (%g%s 刻み) で %.2f%s 動かして接触。"
                "離脱して %g%s 刻みで寄せ直す",
                spec.name,
                homing.coarse_step,
                spec.unit,
                travelled,
                spec.unit,
                homing.step,
                spec.unit,
            )
            # 粗い 1 歩が ON 区間より広いと、当てた時点でもう区間の外 (機構端の側) に
            # 居る。そのまま寄せ直すと探索方向へ走り抜けるので動かす前に問い直す
            if not any(self._sensor_active(name) is True for name in homing.sensor_names):
                raise HomingError(
                    f"軸 '{spec.name}' は粗探索 ({homing.coarse_step}{spec.unit} 刻み) の"
                    f" 1 歩で原点センサ {_label(homing.sensor_names)} の ON 区間を"
                    "跨ぎ切りました (当てた直後にもう OFF)。このまま寄せ直すと探索方向へ"
                    "走り抜けるので止めます。"
                    "homing.coarse_step を ON 区間の実測より狭くしてください"
                )
            released += await self._release(spec, handle, homing, latches)

        start = self._observe(spec, handle)
        remaining = _remaining_distance(homing, travelled=travelled, released=released)
        observed = await self._seek(
            spec,
            handle,
            homing,
            latches,
            step=homing.step,
            direction=homing.direction,
            want_active=True,
            limit=remaining,
            limit_message=_not_reached_message(spec, homing, remaining),
        )
        travelled += abs(observed - start)

        logger.info("[homing] %s: %.2f%s 動かして原点に到達", spec.name, travelled, spec.unit)
        if homing.sensors is not None:
            await self._align(spec, handle, homing, homing.sensors, latches)
        await self._capture_origin(spec.name)
        return travelled

    async def _release(
        self,
        spec: AxisSpec,
        handle: AxisHandle,
        homing: HomingSpec,
        latches: _SensorLatches,
    ) -> float:
        """ON 区間の外まで**探索と逆向きに**離れ、実測で戻った距離を返す。

        触れたその場を原点にすると「区間のどこで始めたか」がそのまま原点の
        ばらつきになる。刻みが常に粗い側 (`coarse_step or step`) なのは、粗探索が
        ON 区間を跨ぎ切ったときにも区間の手前まで戻れるようにするため
        (細かい刻みだと跨いだ先から 1 歩しか戻らず、寄せ直しが区間から離れる向きへ
        走り出す)。
        """
        limit = _release_limit(homing)
        start = self._observe(spec, handle)
        observed = await self._seek(
            spec,
            handle,
            homing,
            latches,
            step=homing.coarse_step or homing.step,
            direction=-homing.direction,
            want_active=False,
            limit=limit,
            limit_message=(
                f"軸 '{spec.name}' を原点センサ {_label(homing.sensor_names)} から"
                "離せませんでした"
                f" ({limit}{spec.unit} 動かしても OFF に"
                " ならない)。**センサの極性が逆だとどこへ動かしても ON のまま**に"
                " なるので、ファーム側の極性設定 (sensorActiveLow) を"
                "接点の固着・配線の短絡と併せて確認してください。"
                " 極性が正しいなら ON 区間がこの距離より広いので"
                " homing.release_distance を実測へ広げてください"
            ),
        )
        return abs(observed - start)

    async def _align(
        self,
        spec: AxisSpec,
        handle: AxisHandle,
        homing: HomingSpec,
        sensors: Mapping[str, str],
        latches: _SensorLatches,
    ) -> None:
        """左右に 1 本ずつスイッチが付く軸で、まだ当たっていない側だけを進める。"""
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
        progress = _progress_threshold(homing.step)

        while True:
            await self._stop_here_if_lost_each(spec, handle, homing)

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
        """片側だけを進める段の唯一の無人の歯止め。"""
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
        latches: _SensorLatches,
        sensors: Mapping[str, str],
        pending: list[str],
        commanded: Mapping[str, float],
    ) -> None:
        reached = _progress_threshold(homing.step)
        for _ in range(_FOLLOW_ATTEMPTS):
            await self._sleep(homing.settle_s)
            latches.poll()
            if any(latches.latched(sensors[motor]) for motor in pending):
                return
            observed = self._observe_each(spec, handle)
            if all(abs(observed[motor] - commanded[motor]) <= reached for motor in pending):
                return

    def _log_sensor_states(self, spec: AxisSpec, homing: HomingSpec) -> None:
        states = {True: "ON", False: "OFF", None: "読めず"}
        logger.info(
            "[homing] %s: 原点確定 (センサ現在値: %s)",
            spec.name,
            ", ".join(
                f"{name}={states[self._sensor_active(name)]}" for name in homing.sensor_names
            ),
        )

    async def _seek(
        self,
        spec: AxisSpec,
        handle: AxisHandle,
        homing: HomingSpec,
        latches: _SensorLatches,
        *,
        step: float,
        direction: float,
        want_active: bool,
        limit: float,
        limit_message: str,
    ) -> float:
        """センサが `want_active` になるまで `direction` 方向へ `step` ずつ動かす。

        **探索・離脱・二段探索の各段がこの 1 本を通る。** 歯止めを向きごとに書き分けると
        「探索は止まるのに離脱は永久に動き続ける」形が作れる。
        """
        if want_active:
            # 離脱段や前回の零点確定で溜まったラッチが残っていると 1 歩目で到達と読む。
            # ラッチを提供しないセンサもここで弾く (ラッチは読むと消えるので、
            # 唯一の読み手であるこの捨てる場所以外から問えない)
            unsupported = latches.discard()
            if unsupported:
                raise HomingError(
                    f"軸 '{spec.name}' の原点センサ {_label(unsupported)} は接触のラッチを"
                    "提供しません (現在値だけでは指令 1 回ぶんの通過を取りこぼすので"
                    "探索を開始しません)"
                )

        start = self._observe(spec, handle)
        observed = start
        stalled = 0
        while True:
            # 鮮度は 1 歩ごとに問い直す —— センサの読み口は途絶しても最後に届いた
            # フラグを返し続けるので、探索中の途絶は「いつまでも当たらない」形にしか
            # ならない。距離超過より先に問うのは、そちらが原因でこちらが症状だから
            lost = self._feedback_lost(spec, homing)
            if lost is not None:
                # 最後に送った「実測 + step」が生きたままだとドライバ内蔵の位置ループが
                # 押し続ける (接触を検出したときと同じ作法)
                await handle.set_target_value(spec.to_commands(observed))
                raise HomingError(lost)

            if abs(observed - start) >= limit:
                raise HomingError(limit_message)

            # 毎回そのときの実測位置へアンカーし直す。指令の積算で組むと、追従が
            # 遅れているあいだ指令だけが先行して機構に大きな偏差が掛かり続ける
            commanded = observed + direction * step
            await handle.set_target_value(spec.to_commands(commanded))

            hit = await self._wait_step(
                spec, handle, homing, latches, commanded, step=step, want_active=want_active
            )

            previous = observed
            observed = self._observe(spec, handle)

            if hit:
                # 原点確定 (disable) が届くまでスイッチを越えた先へ向かい続けないよう、
                # 検出位置を目標に送り直す
                await handle.set_target_value(spec.to_commands(observed))
                return observed

            # 指令を実測へ再アンカーしている以上、引っかかった機構は実測の移動量で
            # 数える上限へ永久に届かない。進まないことそのものを失敗として扱う
            progress = _progress_threshold(step)
            stalled = 0 if abs(observed - previous) >= progress else stalled + 1
            if stalled >= _STALL_LIMIT:
                raise HomingError(
                    f"軸 '{spec.name}' が指令しても動きません"
                    f" ({_STALL_LIMIT} 歩連続で {progress}{spec.unit} 進まなかった)。"
                    " 機構の引っかかり・探索方向・モータの励磁を確認してください"
                )

    def _check_preconditions(self, spec: AxisSpec, homing: HomingSpec) -> None:
        """**1 歩も動かす前に**、止められない探索になっていないかを確かめる。"""
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

        # 探索中の再確認と同じ 1 本を使う (文言を書き分けると、事前と途中で
        # 同じ事象が別の原因に見える)
        lost = self._feedback_lost(spec, homing)
        if lost is not None:
            raise HomingError(lost)

        # **`False` (無励磁だと分かっている) のときだけ降りる。** `None` は
        # 「励磁状態を報告しないドライバ」なので倒すと M3508 の軸が零点確定できない
        unenergized = [name for name in spec.motor_names if self._motor_is_energized(name) is False]
        if unenergized:
            raise HomingError(
                f"軸 '{spec.name}' のモータ {', '.join(unenergized)} が無励磁です"
                " (緊急停止の解除・再励磁を済ませてから零点確定してください)"
            )

    def _feedback_lost(self, spec: AxisSpec, homing: HomingSpec) -> str | None:
        """センサまたは対象軸のフィードバックが途絶していれば、その理由。"""
        stale_sensors = [name for name in homing.sensor_names if self._sensor_is_stale(name)]
        if stale_sensors:
            return (
                f"軸 '{spec.name}' の原点センサ {_label(stale_sensors)} が応答していません"
                " (配線・基板の電源を確認してください)"
            )

        stale = [name for name in spec.motor_names if self._motor_is_stale(name)]
        if stale:
            return (
                f"軸 '{spec.name}' の現在位置を読めません"
                f" (モータ {', '.join(stale)} のフィードバックが途絶しています)"
            )
        return None

    async def _stop_here_if_lost_each(
        self, spec: AxisSpec, handle: AxisHandle, homing: HomingSpec
    ) -> None:
        """整列段の 1 歩ごとの途絶確認。降りる前にその場の実測位置を目標へ送り直す。"""
        lost = self._feedback_lost(spec, homing)
        if lost is None:
            return
        await handle.set_target_value(spec.to_commands_each(self._observe_each(spec, handle)))
        raise HomingError(lost)

    async def _wait_step(
        self,
        spec: AxisSpec,
        handle: AxisHandle,
        homing: HomingSpec,
        latches: _SensorLatches,
        commanded: float,
        *,
        step: float,
        want_active: bool,
    ) -> bool:
        """1 歩ぶんの追従を待つ。待っている間にセンサが `want_active` になったら True。"""
        # **`spec.tolerance` を流用してはならない。** 指令は実測 + step で組むので、
        # 1 歩も動いていないときの差はちょうど step —— `step <= tolerance` の軸では
        # 動く前に必ず追従完了になり、正常な機構が停滞判定で落ちる (実機で発生)
        reached = _progress_threshold(step)
        for _ in range(_FOLLOW_ATTEMPTS):
            await self._sleep(homing.settle_s)
            if self._sensor_reached(homing, latches, want_active=want_active):
                return True
            if abs(self._observe(spec, handle) - commanded) <= reached:
                return False
        return False

    def _sensor_reached(
        self, homing: HomingSpec, latches: _SensorLatches, *, want_active: bool
    ) -> bool:
        """**探索と離脱で見るものが違う。対称に見えて非対称である。**

        探索はラッチ (「一度でも ON になったか」) を見る —— ON 区間が `step` より
        狭いと現在値では通過を取りこぼし、そのままスイッチを越えて機構の破損側へ
        進み続ける。離脱に同じ形 (「一度でも OFF になったか」) を持ち込むと、接点の
        チャタリングで OFF が 1 回混じっただけで ON 区間の中を原点にする。
        取りこぼしの向きも非対称で、離脱の取りこぼしは次の探索が寄せ直す。
        """
        if want_active:
            return latches.any_latched()
        return all(self._sensor_active(name) is False for name in homing.sensor_names)

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


@dataclass(frozen=True)
class AxisHomingResult:
    axis: str
    error: str | None


def homing_axis_names(table: PositionTable) -> list[str]:
    return [name for name in table.axes if table.axis(name).homing is not None]


async def run_homing(
    runner: HomingRunner,
    table: PositionTable,
    motors: MotorGroup,
    *,
    court: Court,
    axes: Collection[str] | None = None,
    on_axis: Callable[[str], Awaitable[None]] | None = None,
    on_result: Callable[[AxisHomingResult], Awaitable[None]] | None = None,
    stop_on_error: bool = True,
) -> list[AxisHomingResult]:
    """`homing:` を持つ軸を順に寄せて零点を確定する。**動作確認と単独実行が通る唯一の経路。**

    `stop_on_error=False` は 1 本の失敗で残りを諦めない。軸ごとに独立した確定なので、
    操縦者は 1 回の実行で全軸の可否を知りたい。
    """
    targets = homing_axis_names(table) if axes is None else list(axes)
    if not targets:
        logger.info("零点確定: homing を持つ軸が無いため飛ばす")
        return []

    results: list[AxisHomingResult] = []
    for axis in targets:
        spec = table.axis(axis).for_court(court)
        logger.info("零点確定: %s", axis)
        if on_axis is not None:
            await on_axis(axis)
        handle = AxisHandle(
            spec,
            [getattr(motors, name) for name in spec.motor_names],
            sensor_active=motors.sensor_active,
        )
        try:
            await runner.home(spec, handle)
        except Exception as exc:
            if stop_on_error:
                raise
            logger.error("零点確定に失敗: %s (%s)", axis, exc)
            result = AxisHomingResult(axis=axis, error=str(exc))
        else:
            result = AxisHomingResult(axis=axis, error=None)
        results.append(result)
        if on_result is not None:
            await on_result(result)
    return results
