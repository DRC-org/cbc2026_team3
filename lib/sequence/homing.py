from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, replace

from lib.drivers.base import ControlMode
from lib.match_state import Court
from lib.sequence.motors import AxisHandle, MotorGroup
from lib.sequence.positions import AxisSpec, HomingSpec, PositionTable

logger = logging.getLogger(__name__)

__all__ = [
    "AxisHomingResult",
    "HomingError",
    "HomingRunner",
    "SwitchMeasurement",
    "homing_axis_names",
    "homing_order",
    "measure_switch",
    "run_homing",
]

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
#: 接触 (OFF→ON) の累計。**単調増加で読んでも減らない**ので読み手が何人いても壊れない。
#: `None` = カウンタを提供しないドライバ
SensorContactCount = Callable[[str], int | None]
SensorStale = Callable[[str], bool]
MotorStale = Callable[[str], bool]
#: 三値。`None` = 励磁を報告しないドライバ (M3508) であって無励磁ではない
MotorEnergized = Callable[[str], bool | None]
OriginCapturable = Callable[[str], bool]
CaptureOrigin = Callable[[str], Awaitable[None]]
SleepFunc = Callable[[float], Awaitable[None]]
#: 整列段のあいだ、指定したセンサを可動端の歯止めから外す
#: (`lib.motion_guard.SensorSuspension.suspend`)
SuspendSensors = Callable[[Iterable[str]], contextlib.AbstractContextManager[None]]
#: 位置名で軸を寄せる口 (`Sequence.move_to`)。零点確定は自分では組まず、必ず注入で受ける
MoveTo = Callable[[Mapping[str, str]], Awaitable[None]]


def _no_suspension(_names: Iterable[str]) -> contextlib.AbstractContextManager[None]:
    """配線しなかった場合。**歯止めは掛かったままなので安全側に倒れる。**

    左右にスイッチを持つ軸の整列段は `GuardViolation` で必ず失敗するので、
    配線し忘れは「黙って守りが消える」ではなく「その軸だけ零点確定できない」
    という形で表に出る。
    """
    return contextlib.nullcontext()


class _SensorContacts:
    """基準値からの増分を「前回見てから接触したか」と読む。

    カウンタは読んでも減らないので、同じセンサを読む常駐監視が別にいても
    こちらのぶんが消えない。**複数センサを 1 回の観測でまとめて読む**性質は
    整列段が「どちらが先に押されたか」を持ち越すために要る。
    """

    def __init__(self, sensors: tuple[str, ...], read: SensorContactCount) -> None:
        self._sensors = sensors
        self._read = read
        self._baseline: dict[str, int] = dict.fromkeys(sensors, 0)
        self._contacted: dict[str, bool] = dict.fromkeys(sensors, False)

    def poll(self) -> None:
        for name in self._sensors:
            count = self._read(name)
            if count is not None and count > self._baseline[name]:
                self._contacted[name] = True

    def rebase(self) -> tuple[str, ...]:
        """基準値を今の値へ取り直し、カウンタを提供しなかったセンサ名を返す。"""
        unsupported = []
        for name in self._sensors:
            count = self._read(name)
            if count is None:
                unsupported.append(name)
            self._baseline[name] = count or 0
            self._contacted[name] = False
        return tuple(unsupported)

    def any_contacted(self) -> bool:
        self.poll()
        return any(self._contacted.values())

    def contacted(self, sensor: str) -> bool:
        return self._contacted[sensor]


def _label(names: Iterable[str]) -> str:
    return " / ".join(f"'{name}'" for name in names)


def _release_limit(homing: HomingSpec) -> float:
    # 本来は ON 区間の広さで決まる値。step の倍数を既定にしてあるのは既存の軸を
    # 変えないためだけで、精度のために step を詰める軸ほど明示が要る
    if homing.release_distance is not None:
        return homing.release_distance
    return homing.step * _RELEASE_STEP_LIMIT


def _align_step(homing: HomingSpec) -> float:
    """整列段 1 歩の刻み [軸の unit]。**探索段の `step` とは別に決まる。**

    探索段は左右 2 台で押すのに整列段は 1 台なので、同じ刻みでは同じ押しが出ず、
    静止摩擦の手前で止まる。書かない軸は `step` と同じ。
    """
    if homing.align_step is not None:
        return homing.align_step
    return homing.step


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


@dataclass(frozen=True)
class SwitchMeasurement:
    """スイッチ 1 本ぶんの実測。距離は軸の `unit`。"""

    axis: str
    unit: str
    direction: float
    engage: float
    release: float
    width: float
    #: 作動点のばらつきは刻みそのもの。値と一緒に配らないと精度が読めない
    step: float
    coarse_step: float | None

    def to_dict(self) -> dict[str, object]:
        return {
            "axis": self.axis,
            "unit": self.unit,
            "direction": self.direction,
            "engage": self.engage,
            "release": self.release,
            "width": self.width,
            "step": self.step,
            "coarse_step": self.coarse_step,
        }


def _probe_spec(
    spec: AxisSpec,
    homing: HomingSpec,
    direction: float,
    step: float | None,
    coarse_step: float | None,
    limit: float | None,
) -> HomingSpec:
    """測定用に向きと刻みだけ差し替えた `HomingSpec`。

    **上限は `search_distance` として載せる。** 別の変数で持つと探索の各段が見る
    歯止めと測定の歯止めが二重になり、片方だけが直った状態が作れる。
    `HomingSpec` の検証もそのまま効く。
    """
    changes: dict[str, object] = {"direction": float(direction)}
    if step is not None:
        changes["step"] = step
    if coarse_step is not None:
        changes["coarse_step"] = coarse_step
    if limit is not None:
        changes["search_distance"] = limit
    try:
        return replace(homing, **changes)  # type: ignore[arg-type]
    except ValueError as exc:
        raise HomingError(f"軸 '{spec.name}' の測定条件が成り立ちません ({exc})") from exc


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
        sensor_contact_count: SensorContactCount,
        sensor_is_stale: SensorStale,
        motor_is_stale: MotorStale,
        motor_is_energized: MotorEnergized,
        origin_capturable: OriginCapturable,
        capture_origin: CaptureOrigin,
        suspend_sensors: SuspendSensors = _no_suspension,
        sleep: SleepFunc = asyncio.sleep,
    ) -> None:
        self._sensor_active = sensor_active
        self._sensor_contact_count = sensor_contact_count
        self._sensor_is_stale = sensor_is_stale
        self._motor_is_stale = motor_is_stale
        self._motor_is_energized = motor_is_energized
        self._origin_capturable = origin_capturable
        self._capture_origin = capture_origin
        self._suspend_sensors = suspend_sensors
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
        contacts = _SensorContacts(homing.sensor_names, self._sensor_contact_count)
        travelled = await self._approach(spec, handle, homing, contacts)

        logger.info("[homing] %s: %.2f%s 動かして原点に到達", spec.name, travelled, spec.unit)
        if homing.sensors is not None:
            await self._align(spec, handle, homing, homing.sensors, contacts)
        await self._capture_origin(spec.name)
        return travelled

    async def measure(
        self,
        spec: AxisSpec,
        handle: AxisHandle,
        *,
        direction: float,
        step: float | None = None,
        coarse_step: float | None = None,
        limit: float | None = None,
    ) -> SwitchMeasurement:
        """`direction` の側の端まで寄せ、スイッチの作動点と離脱点を測る。

        零点確定と**同じ二段探索を通り、原点を書き込む段だけを行わない**。手順を
        書き写すと、片方だけが直された状態が作れる。
        """
        homing = spec.homing
        if homing is None:
            raise HomingError(
                f"軸 '{spec.name}' に homing 設定がありません"
                " (どのセンサを見るか・どこまで動かしてよいかが決まりません)"
            )

        probe = _probe_spec(spec, homing, direction, step, coarse_step, limit)
        self._check_preconditions(spec, probe, require_origin=False)
        contacts = _SensorContacts(probe.sensor_names, self._sensor_contact_count)

        await self._approach(spec, handle, probe, contacts)
        engage = self._observe(spec, handle)
        release = await self._release(spec, handle, probe, contacts)

        result = SwitchMeasurement(
            axis=spec.name,
            unit=spec.unit,
            direction=probe.direction,
            engage=engage,
            release=release,
            width=abs(release - engage),
            step=probe.step,
            coarse_step=probe.coarse_step,
        )
        logger.info(
            "[switch] %s: 作動点 %.3f%s / 離脱点 %.3f%s / ON 区間 %.3f%s (刻み %g%s)",
            spec.name,
            result.engage,
            spec.unit,
            result.release,
            spec.unit,
            result.width,
            spec.unit,
            result.step,
            spec.unit,
        )
        return result

    async def _approach(
        self,
        spec: AxisSpec,
        handle: AxisHandle,
        homing: HomingSpec,
        contacts: _SensorContacts,
    ) -> float:
        """`homing.direction` の側の端へ寄せ、**探索で**動いた距離を返す。

        二段探索では 2 段の合計で、離脱のぶんは含めない。
        """
        # 離脱で戻ったぶんは探索距離を消費しない (理由は `_remaining_distance`)
        released = 0.0
        travelled = 0.0

        # ここが問うのは「**今**触れているか」。接触の累計で問うと、前回の零点確定や
        # 手動操縦でスイッチを跨いだ痕跡だけで離脱段へ入る
        if any(self._sensor_active(name) is True for name in homing.sensor_names):
            logger.info("[homing] %s: 既にセンサに触れているため一度離れて寄せ直す", spec.name)
            released += await self._release_by(spec, handle, homing, contacts)

        if homing.coarse_step is not None:
            start = self._observe(spec, handle)
            limit = _remaining_distance(homing, travelled=travelled, released=released)
            observed = await self._seek(
                spec,
                handle,
                homing,
                contacts,
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
            released += await self._release_by(spec, handle, homing, contacts)

        start = self._observe(spec, handle)
        remaining = _remaining_distance(homing, travelled=travelled, released=released)
        observed = await self._seek(
            spec,
            handle,
            homing,
            contacts,
            step=homing.step,
            direction=homing.direction,
            want_active=True,
            limit=remaining,
            limit_message=_not_reached_message(spec, homing, remaining),
        )
        travelled += abs(observed - start)
        return travelled

    async def _release_by(
        self,
        spec: AxisSpec,
        handle: AxisHandle,
        homing: HomingSpec,
        contacts: _SensorContacts,
    ) -> float:
        """離脱で戻った距離。"""
        start = self._observe(spec, handle)
        return abs(await self._release(spec, handle, homing, contacts) - start)

    async def _release(
        self,
        spec: AxisSpec,
        handle: AxisHandle,
        homing: HomingSpec,
        contacts: _SensorContacts,
    ) -> float:
        """ON 区間の外まで**探索と逆向きに**離れ、OFF になった位置を返す。

        触れたその場を原点にすると「区間のどこで始めたか」がそのまま原点の
        ばらつきになる。刻みが常に粗い側 (`coarse_step or step`) なのは、粗探索が
        ON 区間を跨ぎ切ったときにも区間の手前まで戻れるようにするため
        (細かい刻みだと跨いだ先から 1 歩しか戻らず、寄せ直しが区間から離れる向きへ
        走り出す)。
        """
        limit = _release_limit(homing)
        return await self._seek(
            spec,
            handle,
            homing,
            contacts,
            step=homing.coarse_step or homing.step,
            direction=-homing.direction,
            want_active=False,
            limit=limit,
            limit_message=(
                f"軸 '{spec.name}' を原点センサ {_label(homing.sensor_names)} から"
                "離せませんでした"
                f" ({limit}{spec.unit} 動かしても OFF に"
                " ならない)。まずセンサの極性を疑ってください —— 逆だとどこへ動かしても"
                " ON のままになります。ファーム側の極性設定 (sensorActiveLow) を"
                "接点の固着・配線の短絡と併せて確認してください。"
                " 極性が正しいなら ON 区間がこの距離より広いので"
                " homing.release_distance を実測へ広げてください"
            ),
        )

    async def _align(
        self,
        spec: AxisSpec,
        handle: AxisHandle,
        homing: HomingSpec,
        sensors: Mapping[str, str],
        contacts: _SensorContacts,
    ) -> None:
        """左右に 1 本ずつスイッチが付く軸で、まだ当たっていない側だけを進める。

        **この段のあいだだけ、この軸の原点センサを可動端の歯止めから外す。**
        軸としては端へ向かう向きなので、既に押された 1 本を見た歯止め (指令の入口と
        50Hz の常駐監視) がこの段の指令を拒否し、整列段は必ず失敗する。進める側の
        スイッチはまだ押されていないので機構から見れば進んでよい ——
        判断と外す範囲の理由は `lib.motion_guard.SensorSuspension` が持つ。
        """
        with self._suspend_sensors(homing.sensor_names):
            await self._align_pending(spec, handle, homing, sensors, contacts)

    async def _align_pending(
        self,
        spec: AxisSpec,
        handle: AxisHandle,
        homing: HomingSpec,
        sensors: Mapping[str, str],
        contacts: _SensorContacts,
    ) -> None:
        pending = [motor for motor, sensor in sensors.items() if not contacts.contacted(sensor)]
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
        progress = _progress_threshold(_align_step(homing))

        while True:
            await self._stop_here_if_lost_each(spec, handle, homing)

            contacts.poll()
            observed = self._observe_each(spec, handle)
            for motor in [motor for motor in pending if contacts.contacted(sensors[motor])]:
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
            await self._wait_align_step(spec, handle, homing, contacts, sensors, pending, commanded)

            moved = self._observe_each(spec, handle)
            for motor in pending:
                stalled[motor] = (
                    0 if abs(moved[motor] - observed[motor]) >= progress else stalled[motor] + 1
                )
                if stalled[motor] >= _STALL_LIMIT:
                    states = self._sensor_states(homing)
                    logger.error("[homing] %s: 整列段で停滞 (センサ現在値: %s)", spec.name, states)
                    raise HomingError(
                        self._align_stall_message(
                            spec,
                            motor,
                            states=states,
                            progress=progress,
                            pending=pending,
                            commanded=commanded,
                            moved=moved,
                            start=start,
                        )
                    )

    def _align_stall_message(
        self,
        spec: AxisSpec,
        motor: str,
        *,
        states: str,
        progress: float,
        pending: list[str],
        commanded: Mapping[str, float],
        moved: Mapping[str, float],
        start: Mapping[str, float],
    ) -> str:
        """停滞で降りるときに操縦者へ渡す判断材料。

        原因は 4 通りあり文言だけでは切り分けられない。**保持側が引きずられた量**が
        遊びの有無を分け、**指令値と実測値の差**が押しの届かなさを表す。
        """
        holding = [name for name in spec.motor_names if name not in pending]
        dragged = (
            ", ".join(f"{name} {moved[name] - start[name]:+.2f}{spec.unit}" for name in holding)
            if holding
            else "なし"
        )
        return (
            f"軸 '{spec.name}' の整列段でモータ '{motor}' が指令しても動きません"
            f" ({_STALL_LIMIT} 歩連続で {progress}{spec.unit} 進まなかった)。"
            f" センサ現在値: {states}。"
            f" この段で {motor} は {moved[motor] - start[motor]:+.2f}{spec.unit} 進み、"
            f"直前の 1 歩は指令 {commanded[motor]:.2f}{spec.unit} に対し"
            f" 実測 {moved[motor]:.2f}{spec.unit}。"
            f" 保持側の引きずられ量: {dragged}。"
            "機構に遊びが無いと片側だけを動かせず、相方を引きずったまま止まって"
            "見えます —— 1 台では静止摩擦を越えられていない"
            " (homing.align_step を大きくする)・機構の引っかかり・モータの励磁を"
            "確認してください"
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
        step = _align_step(homing)
        values = {
            motor: (observed[motor] + homing.direction * step if motor in pending else hold[motor])
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
                "まずセンサの極性を疑ってください —— 逆だと押しても ON になりません。"
                "ファーム側の極性設定 (sensorActiveLow) と、"
                "スイッチの配線・断線を確認してください"
            )

    async def _wait_align_step(
        self,
        spec: AxisSpec,
        handle: AxisHandle,
        homing: HomingSpec,
        contacts: _SensorContacts,
        sensors: Mapping[str, str],
        pending: list[str],
        commanded: Mapping[str, float],
    ) -> None:
        reached = _progress_threshold(_align_step(homing))
        for _ in range(_FOLLOW_ATTEMPTS):
            await self._sleep(homing.settle_s)
            contacts.poll()
            if any(contacts.contacted(sensors[motor]) for motor in pending):
                return
            observed = self._observe_each(spec, handle)
            if all(abs(observed[motor] - commanded[motor]) <= reached for motor in pending):
                return

    def _sensor_states(self, homing: HomingSpec) -> str:
        """原点センサの現在値。**三値のまま出す** —— `None` を `OFF` へ丸めると
        途絶したセンサが「離れている」に見え、成功時のログと失敗時の文言で
        同じ事象が別の状態に読める。
        """
        states = {True: "ON", False: "OFF", None: "読めず"}
        return ", ".join(
            f"{name}={states[self._sensor_active(name)]}" for name in homing.sensor_names
        )

    def _log_sensor_states(self, spec: AxisSpec, homing: HomingSpec) -> None:
        logger.info(
            "[homing] %s: 原点確定 (センサ現在値: %s)", spec.name, self._sensor_states(homing)
        )

    async def _seek(
        self,
        spec: AxisSpec,
        handle: AxisHandle,
        homing: HomingSpec,
        contacts: _SensorContacts,
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
            # 離脱段や前回の零点確定で数えたぶんまで見ていると 1 歩目で到達と読む。
            # カウンタを提供しないセンサもここで弾く
            unsupported = contacts.rebase()
            if unsupported:
                raise HomingError(
                    f"軸 '{spec.name}' の原点センサ {_label(unsupported)} は接触のカウンタを"
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
                await self._stop_here(spec, handle)
                raise HomingError(lost)

            if abs(observed - start) >= limit:
                raise HomingError(limit_message)

            # 毎回そのときの実測位置へアンカーし直す。指令の積算で組むと、追従が
            # 遅れているあいだ指令だけが先行して機構に大きな偏差が掛かり続ける
            commanded = observed + direction * step
            await handle.set_target_value(spec.to_commands(commanded))

            hit = await self._wait_step(
                spec, handle, homing, contacts, commanded, step=step, want_active=want_active
            )

            previous = observed
            observed = self._observe(spec, handle)

            if hit:
                # 原点確定 (disable) が届くまでスイッチを越えた先へ向かい続けないよう、
                # 検出位置を目標に送り直す
                await self._stop_here(spec, handle)
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

    def _check_preconditions(
        self, spec: AxisSpec, homing: HomingSpec, *, require_origin: bool = True
    ) -> None:
        """**1 歩も動かす前に**、止められない探索になっていないかを確かめる。"""
        if spec.command_mode is not ControlMode.POSITION:
            raise HomingError(
                f"軸 '{spec.name}' は位置指令ではないため探索できません"
                f" (command_mode={spec.command_mode.value})"
            )

        # 原点を書き込まない測定はここを問わない。問うと「零点を確定できない軸ほど
        # 作動点を測りたい」という当たり前の場面で測れなくなる
        if require_origin and not self._origin_capturable(spec.name):
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

    async def _stop_here(self, spec: AxisSpec, handle: AxisHandle) -> None:
        """「その場で止まれ」。**実測を指令の単位のまま書き戻す唯一の口。**

        値へ換算して戻すと (`to_commands(to_value(…))`) 複数モータ軸では平均を挟む
        ぶん往復が丸め誤差を生み、それが非ゼロの `delta` として可動端の歯止めへ
        届く —— **止めるための指令が、止まっていないことを理由に拒否される**
        (`AxisHandle.observed_commands` と `LimitMonitor._stop_here` が同じ理由で
        同じ形をしている)。平均へ寄せないので、各モータは自分の位置を保持する。
        """
        try:
            commands = handle.observed_commands()
        except Exception as exc:
            raise HomingError(f"軸 '{spec.name}' の現在位置を読めません ({exc})") from exc
        await handle.set_target_value(commands)

    async def _stop_here_if_lost_each(
        self, spec: AxisSpec, handle: AxisHandle, homing: HomingSpec
    ) -> None:
        """整列段の 1 歩ごとの途絶確認。降りる前にその場の実測位置を目標へ送り直す。"""
        lost = self._feedback_lost(spec, homing)
        if lost is None:
            return
        await self._stop_here(spec, handle)
        raise HomingError(lost)

    async def _wait_step(
        self,
        spec: AxisSpec,
        handle: AxisHandle,
        homing: HomingSpec,
        contacts: _SensorContacts,
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
            if self._sensor_reached(homing, contacts, want_active=want_active):
                return True
            if abs(self._observe(spec, handle) - commanded) <= reached:
                return False
        return False

    def _sensor_reached(
        self, homing: HomingSpec, contacts: _SensorContacts, *, want_active: bool
    ) -> bool:
        """**探索と離脱で見るものが違う。対称に見えて非対称である。**

        探索は接触の累計 (「前回見てから ON になったか」) を見る —— ON 区間が `step` より
        狭いと現在値では通過を取りこぼし、そのままスイッチを越えて機構の破損側へ
        進み続ける。離脱に同じ形 (「一度でも OFF になったか」) を持ち込むと、接点の
        チャタリングで OFF が 1 回混じっただけで ON 区間の中を原点にする。
        取りこぼしの向きも非対称で、離脱の取りこぼしは次の探索が寄せ直す。
        """
        if want_active:
            return contacts.any_contacted()
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


def homing_order(table: PositionTable, axes: Collection[str]) -> list[str]:
    """`guard.requires` の**参照先を先に**並べ替える。**余分な移動は 1 つもしない。**

    零点確定は探索の 1 歩ごとに指令の入口を通るので、`requires` を書いた軸は条件の
    軸が区間に居ないと 1 歩も動けない。参照先を後に回すと、その軸の零点が確定して
    いないうちに条件を評価することになり、順序だけが理由で必ず拒否される。

    **並べ替えるだけで、寄せる指令はここから出さない** —— 零点確定は「選んだ軸しか
    動かさない」ことが守りになっている (`docs/invariants.md` §3)。寄せるかどうかは
    経路ごとの判断で、動作確認は寄せ、零点合わせパネルは寄せない。

    参照は読み込み時に非循環であることが保証されている (`_check_requires_acyclic`)。
    """
    requested = list(dict.fromkeys(axes))
    ordered: list[str] = []
    seen: set[str] = set()

    def visit(axis: str) -> None:
        if axis in seen:
            return
        seen.add(axis)
        guard = table.axis(axis).guard
        for required in () if guard is None else guard.requires:
            # 今回回さない軸は並べ替えの対象外 (寄せ直しは呼び出し側の判断)
            if required.axis in requested:
                visit(required.axis)
        ordered.append(axis)

    for axis in requested:
        visit(axis)
    return ordered


def _axis_handle(
    table: PositionTable, motors: MotorGroup, axis: str, court: Court | None
) -> tuple[AxisSpec, AxisHandle]:
    spec = table.axis(axis).for_court(court)
    return spec, AxisHandle(
        spec,
        [getattr(motors, name) for name in spec.motor_names],
        sensor_active=motors.sensor_active,
        # 零点確定も探索の 1 歩ごとに指令の入口を通るので、干渉条件が効く。
        # 配線しないと `requires` を書いた軸だけが 1 歩も探索できない
        axis_state=motors.axis_state,
    )


async def _retreat(
    spec: AxisSpec,
    handle: AxisHandle,
    table: PositionTable,
    *,
    court: Court,
) -> None:
    """零点確定の直後に、原点から離れた位置名へ退避する。

    原点姿勢が他の軸と干渉する軸 (`y_axis` の 0 と `rotate` の 0) では、退避せずに
    次の軸を寄せるとその軸が 1 歩も動けない。**`HomingRunner` ではなくここに置く**
    のは、位置定数の表を持っているのがこちら側だから。
    """
    homing = spec.homing
    if homing is None or homing.retreat_position is None:
        return

    name = homing.retreat_position
    value = table.raw(spec.name, name, court=court)
    await handle.set_target_value(spec.to_commands(value))
    # 指令と待ちのあいだで緊急停止が目標を消した窓を「到達」と読まないため
    # (目標が無ければ到達済みに吸われ、退避していないのに退避できたことになる)
    if not await handle.wait_reached(timeout=spec.timeout_s, expect_target=True):
        raise HomingError(
            f"軸 '{spec.name}' を零点確定後の退避位置 '{name}' ({value}{spec.unit}) へ"
            f" {spec.timeout_s} 秒以内に動かせませんでした"
            " (退避できていないまま次の軸を寄せると機構が干渉します)"
        )
    logger.info("[homing] %s: 零点確定後に '%s' (%.2f%s) へ退避", spec.name, name, value, spec.unit)


async def measure_switch(
    runner: HomingRunner,
    table: PositionTable,
    motors: MotorGroup,
    *,
    court: Court | None,
    axis: str,
    direction: float,
    step: float | None = None,
    coarse_step: float | None = None,
    limit: float | None = None,
) -> SwitchMeasurement:
    """1 軸 1 向きぶんの作動点測定。**零点は書き込まない。**"""
    spec, handle = _axis_handle(table, motors, axis, court)
    logger.info("作動点測定: %s (向き %+g)", axis, direction)
    return await runner.measure(
        spec, handle, direction=direction, step=step, coarse_step=coarse_step, limit=limit
    )


async def run_homing(
    runner: HomingRunner,
    table: PositionTable,
    motors: MotorGroup,
    *,
    court: Court | None,
    axes: Collection[str] | None = None,
    move_to: MoveTo | None = None,
    on_axis: Callable[[str], Awaitable[None]] | None = None,
    on_result: Callable[[AxisHomingResult], Awaitable[None]] | None = None,
    stop_on_error: bool = True,
) -> list[AxisHomingResult]:
    """`homing:` を持つ軸を順に寄せて零点を確定する。**動作確認と単独実行が通る唯一の経路。**

    `stop_on_error=False` は 1 本の失敗で残りを諦めない。軸ごとに独立した確定なので、
    操縦者は 1 回の実行で全軸の可否を知りたい。

    `move_to` を渡すと **①参照される軸を確定 → ②その軸を寄せる → ③残りを確定**の
    3 段で回す。零点確定は `release_distance` ぶん離脱して終わるので、確定しただけの
    軸は `requires` の区間に居ない —— 寄せる段が無いと、参照する側は順序だけを理由に
    必ず拒否される。渡さなければ並べ替えるだけで、1 本も余計に動かさない。

    **寄せるのは「今回選ばれた軸」だけである。** 選ばれていない軸を寄せると
    「零点確定は選んだ軸しか動かさない」が壊れるので、その場合は寄せずに拒否させ、
    文面で手当てを案内する (`docs/invariants.md` §4)。零点合わせパネルで昇降と前後の
    両方を選べば①〜③が回り、前後だけを選べば拒否される。**この非対称が仕様である。**

    **①で確定できなかった軸は寄せない。** 原点が確定していない軸へ位置名で指令すると
    どこへ動くか分からない。
    """
    targets = homing_axis_names(table) if axes is None else list(dict.fromkeys(axes))
    if not targets:
        logger.info("零点確定: homing を持つ軸が無いため飛ばす")
        return []
    targets = homing_order(table, targets)

    prerequisites = {
        axis: position
        for axis, position in table.homing_prerequisites(targets).items()
        if axis in targets
    }
    if move_to is None or not prerequisites:
        return await _home_each(
            runner, table, motors, court, targets, on_axis, on_result, stop_on_error
        )

    # 回すのは prerequisites に載った軸だけ。「今回選ばれた軸か」を判定するのは
    # 上の内包表記 1 箇所で、ここはその結果を依存順に並べ直すだけ
    referenced = homing_order(table, prerequisites)
    results = await _home_each(
        runner, table, motors, court, referenced, on_axis, on_result, stop_on_error
    )
    confirmed = {result.axis for result in results if result.error is None}
    for axis in referenced:
        if axis not in confirmed:
            logger.error(
                "零点確定: 軸 %s の零点が確定していないため %s へ寄せません",
                axis,
                prerequisites[axis],
            )
            continue
        # 1 軸ずつ送る。まとめて 1 通にすると、前提軸どうしに not_with があるとき
        # 自分の指令が自分の歯止めに拒否される
        logger.info("零点確定: %s を %s へ寄せる", axis, prerequisites[axis])
        await move_to({axis: prerequisites[axis]})

    rest = [axis for axis in targets if axis not in prerequisites]
    results.extend(
        await _home_each(runner, table, motors, court, rest, on_axis, on_result, stop_on_error)
    )
    return results


async def _home_each(
    runner: HomingRunner,
    table: PositionTable,
    motors: MotorGroup,
    court: Court | None,
    axes: list[str],
    on_axis: Callable[[str], Awaitable[None]] | None,
    on_result: Callable[[AxisHomingResult], Awaitable[None]] | None,
    stop_on_error: bool,
) -> list[AxisHomingResult]:
    results: list[AxisHomingResult] = []
    for axis in axes:
        spec = table.axis(axis).for_court(court)
        logger.info("零点確定: %s", axis)
        if on_axis is not None:
            await on_axis(axis)
        _, handle = _axis_handle(table, motors, axis, court)
        try:
            await runner.home(spec, handle)
            await _retreat(spec, handle, table, court=court)
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
