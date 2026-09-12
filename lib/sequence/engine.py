from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Collection, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from lib.match_state import Court
from lib.motion_guard import GuardViolation
from lib.sequence.interlock import AxisInterlock
from lib.sequence.motors import AxisHandle
from lib.sequence.positions import AxisSpec, PositionTable

if TYPE_CHECKING:
    from lib.sequence.motors import MotorGroup

logger = logging.getLogger(__name__)


class SequenceTimeoutError(RuntimeError):
    """目標位置に到達しないままタイムアウトした。"""


class LimitInterventionError(SequenceTimeoutError):
    """可動端保護が移動を止めたので、そのステップを失敗として扱った。

    **中身はタイムアウトではない。** 待ち時間が足りなかったのではなく、機構が端に
    着いたので保護が目標を実測へ書き直した。同じ型で運ぶと、操縦者は `timeout_s`
    を伸ばす側を疑い、配線・向き・スケールの食い違いに辿り着けない。

    **`SequenceTimeoutError` の派生にしてあるのは、既存の捕捉経路を素通りさせる
    ため。** 移動の失敗を型で拾う経路 (`Sequence.run` の `except Exception`) は
    どちらも同じ扱いでよく、狭めた型を投げても失敗が握り潰されない。
    """


class AxisSyncError(RuntimeError):
    """左右ペア軸の位置ずれ (sync_tolerance 超過) を検知した。"""


@dataclass
class StepInfo:
    label: str
    method_name: str
    require_trigger: bool
    axes: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ExcludedStep:
    label: str
    missing_axes: tuple[str, ...]

    def to_dict(self) -> dict:
        return {"step": self.label, "missing_axes": list(self.missing_axes)}


@dataclass(frozen=True)
class StepFailure:
    step_index: int
    label: str
    message: str
    #: 可動端の歯止め (入口の拒否か移動中の介入) で止まった失敗。操縦者が歯止めを外して
    #: そのステップだけ走らせ直せる (`Sequence.request_force_step`)
    limit_related: bool = False

    def to_dict(self) -> dict:
        return {
            "step_index": self.step_index,
            "step": self.label,
            "message": self.message,
            "limit_related": self.limit_related,
        }


#: 可動端センサをその間だけ「押されていない」として読ませる口 (`SensorSuspension.suspend`)
SuspendSensors = Callable[[Collection[str]], AbstractContextManager[None]]


@dataclass(frozen=True)
class LimitIntervention:
    """可動端保護がその軸で移動を止めた回数と、直近の理由。

    **回数と理由は 1 組で運ぶ。** 別々に取れる形にすると「回数を見てから理由を
    取り直す」経路が書け、そのあいだに保護が別の軸で発火すると理由だけが
    入れ替わる (どの軸で何が起きたか分からない失敗メッセージになる)。
    """

    count: int
    reason: str | None = None


#: 軸名 → その軸の `LimitIntervention`。**移動中の保護 (`LimitMonitor`) は
#: `lib/control/` に居るので、注入で受けて import の向きを作らない**
LimitInterventions = Callable[[str], LimitIntervention]

NO_LIMIT_INTERVENTION = LimitIntervention(count=0)

#: これ未満の開始遅延は掛けない。到達判定の走査周期 (0.01s) より短く、着地時刻に効かない
_MIN_START_DELAY_S = 0.01


@dataclass(frozen=True)
class _PlannedMove:
    """1 軸ぶんの「これから出す 1 通」。**検査と送信で同じ 1 組を使う。**

    検査のときと送信のときで別々に位置名から引き直すと、そのあいだにコートが
    変わった場合に**検査した値と送った値が別物**になる。
    """

    handle: AxisHandle
    position_name: str
    wait_s: float | None
    commands: dict[str, float]
    #: 軸の unit へ直した目標。同じ指令に入る他の軸の条件を評価するのに使う
    value: float
    #: この移動にかかる見込み [s]。速さを見積もれない軸 (duty など) は None
    duration_s: float | None = None


def _estimated_duration(spec: AxisSpec, handle: AxisHandle, value: float) -> float | None:
    """この移動にかかる見込み [s]。見積もれない軸は None。

    `observed_value()` を引くのは見積もれる軸だけ。duty / on_off の軸は位置
    フィードバックを持たず `PositionLookupError` になる。
    """
    if spec.estimated_duration(0.0) is None:
        return None
    return spec.estimated_duration(abs(value - handle.observed_value()))


def _start_delays(pending: list[_PlannedMove]) -> list[float]:
    """着地時刻を揃えるための開始遅延 [s]。速度は変えず、早い軸の送信だけを遅らせる。

    見積もれた軸が 1 本以下なら揃える相手が居ないので全部 0 (従来どおり全軸即送信)。
    """
    durations = [move.duration_s for move in pending if move.duration_s is not None]
    if len(durations) < 2:
        return [0.0] * len(pending)
    longest = max(durations)
    delays = [
        0.0 if move.duration_s is None else max(0.0, longest - move.duration_s) for move in pending
    ]
    return [delay if delay >= _MIN_START_DELAY_S else 0.0 for delay in delays]


@dataclass(frozen=True)
class _OriginSeat:
    """原点スイッチへの着座を到着とみなす窓 (`homing.origin_error`)。

    home を原点から離せない軸は、原点合わせのばらつきぶん手前でスイッチに当たる。
    窓は事前 (既に着座) と事後 (保護が止めた) の両方がここだけを見る。
    """

    handle: AxisHandle
    unit: str
    sensors: tuple[str, ...]
    direction: float
    error: float
    slack: float

    @classmethod
    def for_target(cls, spec: AxisSpec, handle: AxisHandle, target: float) -> _OriginSeat | None:
        homing = spec.homing
        if homing is None or homing.origin_error is None:
            return None
        seat = cls(
            handle=handle,
            unit=spec.unit,
            sensors=homing.sensor_names,
            direction=homing.direction,
            error=homing.origin_error,
            slack=spec.tolerance or 0.0,
        )
        return seat if seat.distance(target) <= seat.error else None

    def distance(self, value: float) -> float:
        """原点からスイッチの反対側へ測った距離。"""
        return -self.direction * value

    def contains(self, value: float) -> bool:
        return -self.slack <= self.distance(value) <= self.error + self.slack


def step(
    label: str,
    *,
    require_trigger: bool = False,
    axes: Collection[str] | None = None,
) -> Callable:
    declared = frozenset(axes or ())

    def decorator(method: Callable) -> Callable:
        method._step_label = label  # type: ignore[attr-defined]
        method._step_require_trigger = require_trigger  # type: ignore[attr-defined]
        method._step_axes = declared  # type: ignore[attr-defined]
        return method

    return decorator


class Sequence:
    _steps: list[StepInfo]

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        steps: list[StepInfo] = []
        for name, value in cls.__dict__.items():
            if callable(value) and hasattr(value, "_step_label"):
                steps.append(
                    StepInfo(
                        label=value._step_label,
                        method_name=name,
                        require_trigger=value._step_require_trigger,
                        axes=value._step_axes,
                    )
                )
        cls._steps = steps

    def __init__(self, name: str) -> None:
        self.name = name
        self._current_index: int = 0
        self._waiting_trigger: bool = False
        self._running: bool = False
        self._trigger_event: asyncio.Event = asyncio.Event()
        self._stop_event: asyncio.Event = asyncio.Event()
        self._resume_event: asyncio.Event = asyncio.Event()
        self._jump_request: int | None = None
        self._last_error: StepFailure | None = None
        self._court: Court | None = None
        self._motors: MotorGroup | None = None
        self._positions: PositionTable | None = None
        self._available_axes: frozenset[str] | None = None
        self._excluded_steps: tuple[ExcludedStep, ...] = ()
        self._limit_interventions: LimitInterventions | None = None
        self._suspend_sensors: SuspendSensors | None = None
        self._limit_sensors: tuple[str, ...] = ()
        self._force_index: int | None = None

    def bind_motors(self, group: MotorGroup) -> None:
        self._motors = group

    def bind_limit_override(self, suspend: SuspendSensors, sensors: Collection[str]) -> None:
        """可動端で止まったステップを、歯止めを外して走らせ直す口。

        外すのは **そのステップ 1 つのあいだ** だけで、範囲は `guard.limits` に宣言された
        センサ全部 (指令の入口と移動中の監視の両方が同じ覆いを読む)。どのセンサが止めたかを
        失敗から辿り直さないのは、宣言と実物が入れ替わっている疑いのある端でも同じ口で
        抜けられるようにするため。判断そのもの (`MotionGuard.check_limit`) には触らない。
        """
        self._suspend_sensors = suspend
        self._limit_sensors = tuple(dict.fromkeys(sensors))

    @property
    def can_force(self) -> bool:
        return self._suspend_sensors is not None and bool(self._limit_sensors)

    def request_force_step(self, index: int) -> None:
        """index のステップだけ可動端の歯止めを外して走らせ、その後は通常に戻す。"""
        self._force_index = index
        self._request_index(index)

    def bind_limit_interventions(self, interventions: LimitInterventions) -> None:
        self._limit_interventions = interventions

    def _limit_intervention(self, axis: str) -> LimitIntervention:
        if self._limit_interventions is None:
            return NO_LIMIT_INTERVENTION
        return self._limit_interventions(axis)

    def _origin_sensors(self, seat: _OriginSeat) -> list[bool | None]:
        read = self.motors.sensor_active
        # 未配線を False へ倒すと、読めていない原点センサで着座が成立してしまう
        return [None if read is None else read(name) for name in seat.sensors]

    def _already_seated(self, seat: _OriginSeat, target: float) -> bool:
        """原点スイッチに載ったまま、さらに原点側へ指令しようとしているか。"""
        if not any(state is True for state in self._origin_sensors(seat)):
            return False
        position = seat.handle.observed_value()
        if not seat.contains(position) or seat.distance(target) >= seat.distance(position):
            return False
        logger.info(
            "原点スイッチに着座済み: axis=%s, 実測=%.3f%s (原点側へ押し込まずその場で止めます)",
            seat.handle.name,
            position,
            seat.unit,
        )
        return True

    def _accept_seat(self, seat: _OriginSeat) -> bool:
        """保護が止めた位置を着座として到着扱いにしてよいか。"""
        # 読めていない原点センサがあると、止まった理由がスイッチなのか途絶なのか分からない
        if any(state is None for state in self._origin_sensors(seat)):
            return False
        position = seat.handle.observed_value()
        if not seat.contains(position):
            return False
        logger.info(
            "原点スイッチで着座: axis=%s, 実測=%.3f%s "
            "(原点誤差 %s%s の範囲内なので到着とみなします)",
            seat.handle.name,
            position,
            seat.unit,
            seat.error,
            seat.unit,
        )
        return True

    @property
    def has_motors(self) -> bool:
        return self._motors is not None

    @property
    def motors(self) -> MotorGroup:
        if self._motors is None:
            raise RuntimeError(
                f"シーケンス '{self.name}' に MotorGroup が bind されていません "
                "(bind_motors を呼んでください)"
            )
        return self._motors

    def bind_positions(self, table: PositionTable) -> None:
        self._positions = table

    @property
    def positions(self) -> PositionTable:
        if self._positions is None:
            raise RuntimeError(
                f"シーケンス '{self.name}' に PositionTable が bind されていません "
                "(bind_positions を呼んでください)"
            )
        return self._positions

    def restrict_to_axes(self, available: Collection[str]) -> None:
        allowed = frozenset(available)
        self._available_axes = allowed

        kept: list[StepInfo] = []
        excluded: list[ExcludedStep] = []
        for info in type(self)._steps:
            if not info.axes:
                kept.append(info)
                continue
            present = info.axes & allowed
            if present:
                kept.append(replace(info, axes=present))
            else:
                excluded.append(
                    ExcludedStep(label=info.label, missing_axes=tuple(sorted(info.axes - allowed)))
                )
        self._steps = kept
        self._excluded_steps = tuple(excluded)

    @property
    def excluded_steps(self) -> tuple[ExcludedStep, ...]:
        return self._excluded_steps

    def available_targets(self, targets: Mapping[str, str]) -> dict[str, str]:
        if self._available_axes is None:
            return dict(targets)
        return {
            axis: position for axis, position in targets.items() if axis in self._available_axes
        }

    async def move_to(
        self,
        targets: Mapping[str, str],
        *,
        timeout: float | None = None,
        sync: bool = False,
    ) -> None:
        """位置名で指定した軸を同時に動かし、全軸が着くまで待つ。

        `sync=True` は**着地の時刻を揃える**。速度そのものは変えず、早く着く軸の
        送信を「一番遅い軸の所要時間 - 自分の所要時間」だけ遅らせる。速さを
        見積もれない軸 (`motion` も `min_speed` も持たない duty / on_off など) は
        今までどおり即送信する。

        **歯止めに掛かったときの `GuardViolation` は失敗であって拒否ではない。**
        手動操縦は「その 1 指令を出さない」で済むので拒否として返す
        (`RobotServer._run_manual`) が、シーケンスはその姿勢を前提に次の段が
        組まれているので、飛ばして続行はできない。ここでは握り潰さず、
        ステップの失敗として上へ抜けさせる。
        """
        table = self.positions
        # 出荷のシーケンスは並びで干渉を避けているので発火しないはず。
        # 段を書き換えた人が気付くための歯止め
        AxisInterlock(table).check(
            {axis: table.raw(axis, name, court=self.court) for axis, name in targets.items()},
            court=self.court,
            motors=self.motors,
        )
        # 保護は目標を実測位置へ書き直すので `is_reached` は必ず成立する。控えないと
        # 軸が途中に居るままシーケンスだけが先へ進む
        before = {axis: self._limit_intervention(axis) for axis in targets}

        pending: list[_PlannedMove] = []
        seats: dict[str, _OriginSeat] = {}
        for axis, position_name in targets.items():
            spec = table.axis(axis).for_court(self.court)
            handle = AxisHandle(
                spec,
                [getattr(self.motors, name) for name in spec.motor_names],
                sensor_active=self.motors.sensor_active,
                axis_state=self.motors.axis_state,
                pressed_toward=self.motors.pressed_toward,
            )
            commands = table.commands(axis, position_name, court=self.court)
            value = spec.to_value(commands)
            seat = _OriginSeat.for_target(spec, handle, value)
            if seat is not None:
                seats[axis] = seat
                # 入口の歯止めは押されている端へ向かう指令を拒むので、その場で止まれへ置き換える
                if self._already_seated(seat, value):
                    commands = handle.observed_commands()
                    value = spec.to_value(commands)
            pending.append(
                _PlannedMove(
                    handle=handle,
                    position_name=position_name,
                    wait_s=spec.timeout_s if timeout is None else timeout,
                    commands=commands,
                    value=value,
                    duration_s=_estimated_duration(spec, handle, value) if sync else None,
                )
            )

        # **全軸を検査してから 1 通目を出す。** 軸ごとに「検査 → 送信」を回すと、
        # 2 軸目が拒否された時点で 1 軸目は既に走っており、ピッチとオフセットでは
        # それが一番危ない半端な姿勢になる。同じ指令に入る軸の目標は、この指令が
        # 書き終わった後の値で評価する (`pending_targets`)
        planned = {move.handle.name: move.value for move in pending}
        siblings = frozenset(planned)
        for move in pending:
            move.handle.check_not_with(siblings)
            move.handle.check_target_value(move.commands, pending_targets=planned)

        delays = _start_delays(pending)
        for move, delay in zip(pending, delays, strict=True):
            if delay <= 0.0:
                await move.handle.set_target_value(move.commands, pending_targets=planned)

        # 停止要求と競わせる。ステップの境界だけで見ると、長い移動の途中で通常停止を
        # 押しても着くまで止まらない (2026-09-11 実機で「止まるまで長い」)
        waits = [
            asyncio.ensure_future(
                move.handle.wait_reached(timeout=move.wait_s, expect_target=True)
                if delay <= 0.0
                else self._delayed_move(move, planned, delay)
            )
            for move, delay in zip(pending, delays, strict=True)
        ]
        stop_wait = asyncio.ensure_future(self._stop_event.wait())
        try:
            await asyncio.wait([*waits, stop_wait], return_when=asyncio.FIRST_COMPLETED)
            if self._stop_event.is_set():
                for wait in waits:
                    wait.cancel()
                # その場で止める。目標を残すと停止後もそこへ走り続ける
                for move in pending:
                    await move.handle.set_target_value(move.handle.observed_commands())
                return
            results = [await wait for wait in waits]
        finally:
            stop_wait.cancel()
            for wait in waits:
                if not wait.done():
                    wait.cancel()
        # 終了時の状態ではなく回数で見る。接点がバウンドして触れて離れると、軸は
        # 触れた位置で止まったままなのに状態だけが戻る
        stopped: list[str] = []
        for axis, previous in before.items():
            now = self._limit_intervention(axis)
            if now.count == previous.count:
                continue
            seat = seats.get(axis)
            if seat is not None and self._accept_seat(seat):
                continue
            stopped.append(f"{axis}: {now.reason}")
        failed = [
            f"{move.handle.name}->{move.position_name}"
            for move, reached in zip(pending, results, strict=True)
            if not reached
        ]
        # 片方で `raise` すると、両方起きた移動では先に見たほうしか残らない。切り分けは
        # 「止められた軸」と「届かなかった軸」の対応で進むので、片側だけでは辿れない
        reasons: list[str] = []
        if stopped:
            reasons.append(f"可動端保護が移動を止めました ({', '.join(stopped)})")
        if failed:
            reasons.append(f"目標位置に到達しませんでした ({', '.join(failed)})")
        if reasons:
            # 保護が 1 件でも絡めば時間切れではない。単独の失敗は文言が今までと変わらない
            error = LimitInterventionError if stopped else SequenceTimeoutError
            raise error(f"シーケンス '{self.name}': {' / '.join(reasons)}")

        desynced = []
        for move in pending:
            error = move.handle.sync_violation()
            if error is None:
                continue
            allowed = table.sync_tolerance(move.handle.name) or 0.0
            desynced.append(f"{move.handle.name}: 偏差 {error:.3f} > 許容 {allowed:.3f}")
        if desynced:
            raise AxisSyncError(
                f"シーケンス '{self.name}': 軸内のモータ位置がずれています ({', '.join(desynced)})"
            )

    async def _delayed_move(
        self, move: _PlannedMove, planned: Mapping[str, float], delay: float
    ) -> bool:
        """遅れて送り、そのまま到着まで待つ。**送信と待ちを 1 つに束ねる。**

        `wait_reached(expect_target=True)` は目標未設定の状態で待ち始めると即
        `WaitInterruptedError` を投げるので、送る前に待ちを張れない。
        """
        await asyncio.sleep(delay)
        # まだ 1 通も出していない軸が停止後に動き出すのが一番危ない
        if self._stop_event.is_set():
            return True
        # 遅らせているあいだに他の軸が動くので、ここで `GuardViolation` になりうる。
        # 握り潰さず上へ抜けさせるが、**その時点で先に出した軸は既に走っている**
        await move.handle.set_target_value(move.commands, pending_targets=planned)
        return await move.handle.wait_reached(timeout=move.wait_s, expect_target=True)

    @property
    def court(self) -> Court | None:
        """解決に使うコート。**選ばれるまでは `None`。**

        既定を赤にすると、青コートで選び忘れたことがどこにも現れないまま
        コート依存軸だけが鏡に走る。
        """
        return self._court

    def set_court(self, court: Court | None) -> None:
        self._court = court

    @property
    def current_step(self) -> StepInfo | None:
        if 0 <= self._current_index < len(self._steps):
            return self._steps[self._current_index]
        return None

    @property
    def waiting_trigger(self) -> bool:
        return self._waiting_trigger

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def last_error(self) -> StepFailure | None:
        return self._last_error

    @property
    def steps(self) -> tuple[StepInfo, ...]:
        return tuple(self._steps)

    @property
    def steps_info(self) -> list[dict]:
        return [
            {
                "index": i,
                "label": s.label,
                "require_trigger": s.require_trigger,
            }
            for i, s in enumerate(self.steps)
        ]

    @property
    def progress(self) -> dict:
        return {
            "sequence": self.name,
            "current_step": self.current_step.label if self.current_step else None,
            "step_index": self._current_index,
            "total_steps": len(self._steps),
            "waiting_trigger": self._waiting_trigger,
            "running": self._running,
            "steps": self.steps_info,
            "last_error": self._last_error.to_dict() if self._last_error is not None else None,
        }

    def trigger(self) -> None:
        if self._waiting_trigger:
            self._trigger_event.set()

    def request_jump(self, index: int) -> None:
        if not (0 <= index < len(self._steps)):
            return
        self._request_index(index)

    def request_stop(self) -> None:
        self._stop_event.set()
        if self._waiting_trigger:
            self._trigger_event.set()

    def request_start(self) -> None:
        self._request_index(0)

    def _request_index(self, index: int) -> None:
        self._jump_request = index
        if self._running:
            self._trigger_event.set()
        else:
            self._resume_event.set()

    def discard_pending_start(self) -> None:
        # asyncio.Event は set() の時点で待機中の future を解決するので、clear() では
        # 既に待機に入っている 1 回を取り消せない。正は _jump_request が None であること。
        self._resume_event.clear()
        self._jump_request = None

    async def run_forever(self) -> None:
        while True:
            await self._resume_event.wait()
            self._resume_event.clear()
            if self._jump_request is None:
                continue
            try:
                await self.run()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("シーケンス '%s' の実行中に例外", self.name)
            # 止めたステップの番号は残す。手動で位置を直してから一覧の同じステップで
            # 続きを走らせる (2026-09-12 実機の求め)。先頭へ戻すのは START の役目
            if self._stop_event.is_set():
                self._stop_event.clear()

    async def run(self) -> None:
        self._running = True
        self._stop_event.clear()
        self._last_error = None
        try:
            while not self._stop_event.is_set():
                if self._jump_request is not None:
                    self._current_index = self._jump_request
                    self._jump_request = None
                if self._current_index >= len(self._steps):
                    break
                step_info = self._steps[self._current_index]

                if step_info.require_trigger:
                    self._waiting_trigger = True
                    self._trigger_event.clear()
                    await self._trigger_event.wait()
                    self._waiting_trigger = False
                    if self._stop_event.is_set():
                        break
                    if self._jump_request is not None:
                        continue

                logger.info(
                    "[%s] %d/%d %s",
                    self.name,
                    self._current_index + 1,
                    len(self._steps),
                    step_info.label,
                )

                method = getattr(self, step_info.method_name)
                # 外すのはこの 1 ステップだけ。成否に依らずここで印を消す
                forced = self._force_index == self._current_index and self.can_force
                self._force_index = None
                try:
                    if forced:
                        assert self._suspend_sensors is not None
                        logger.warning(
                            "[%s] %s: 可動端の歯止めを外して実行 (%s を押されていない扱い)",
                            self.name,
                            step_info.label,
                            ", ".join(self._limit_sensors),
                        )
                        with self._suspend_sensors(self._limit_sensors):
                            await method()
                    else:
                        await method()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.exception(
                        "シーケンス '%s' のステップ '%s' で例外", self.name, step_info.label
                    )
                    self._last_error = StepFailure(
                        step_index=self._current_index,
                        label=step_info.label,
                        message=str(exc) or "ステップの実行に失敗しました",
                        limit_related=isinstance(exc, (GuardViolation, LimitInterventionError)),
                    )
                    break

                # 途中で止めたステップは終わっていない。番号を進めると次から再開されて
                # 止めた移動が飛ばされる
                if self._stop_event.is_set():
                    break
                if self._jump_request is None:
                    self._current_index += 1
                    if self._current_index >= len(self._steps):
                        logger.info("[%s] 完走 (%d ステップ)", self.name, len(self._steps))
        finally:
            self._running = False
            self._waiting_trigger = False

    async def reset(self) -> None:
        self._current_index = 0
        self._waiting_trigger = False
        self._running = False
        self._trigger_event.clear()
        self._stop_event.clear()
        self._jump_request = None
        self._force_index = None
        self._last_error = None
