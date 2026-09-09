from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from lib.match_state import Court
from lib.sequence.motors import AxisHandle
from lib.sequence.positions import PositionTable

if TYPE_CHECKING:
    from lib.sequence.motors import MotorGroup

logger = logging.getLogger(__name__)


class SequenceTimeoutError(RuntimeError):
    """目標位置に到達しないままタイムアウトした。"""


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

    def to_dict(self) -> dict:
        return {"step_index": self.step_index, "step": self.label, "message": self.message}


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
        self._court: Court = Court.RED
        self._motors: MotorGroup | None = None
        self._positions: PositionTable | None = None
        self._available_axes: frozenset[str] | None = None
        self._excluded_steps: tuple[ExcludedStep, ...] = ()
        self._limit_interventions: LimitInterventions | None = None

    def bind_motors(self, group: MotorGroup) -> None:
        self._motors = group

    def bind_limit_interventions(self, interventions: LimitInterventions) -> None:
        self._limit_interventions = interventions

    def _limit_intervention(self, axis: str) -> LimitIntervention:
        if self._limit_interventions is None:
            return NO_LIMIT_INTERVENTION
        return self._limit_interventions(axis)

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
    ) -> None:
        table = self.positions
        pending: list[tuple[AxisHandle, str, float | None]] = []
        # 保護は目標を実測位置へ書き直すので `is_reached` は必ず成立する。控えないと
        # 軸が途中に居るままシーケンスだけが先へ進む
        before = {axis: self._limit_intervention(axis) for axis in targets}

        for axis, position_name in targets.items():
            spec = table.axis(axis).for_court(self.court)
            handle = AxisHandle(
                spec,
                [getattr(self.motors, name) for name in spec.motor_names],
                sensor_active=self.motors.sensor_active,
            )
            await handle.set_target_value(
                table.commands(axis, position_name, court=self.court),
            )
            pending.append((handle, position_name, spec.timeout_s if timeout is None else timeout))

        results = await asyncio.gather(
            *(
                handle.wait_reached(timeout=wait_s, expect_target=True)
                for handle, _, wait_s in pending
            )
        )
        # 終了時の状態ではなく回数で見る。接点がバウンドして触れて離れると、軸は
        # 触れた位置で止まったままなのに状態だけが戻る
        stopped = [
            f"{axis}: {now.reason}"
            for axis, previous in before.items()
            if (now := self._limit_intervention(axis)).count != previous.count
        ]
        if stopped:
            raise SequenceTimeoutError(
                f"シーケンス '{self.name}': 可動端保護が移動を止めました ({', '.join(stopped)})"
            )

        failed = [
            f"{handle.name}->{position_name}"
            for (handle, position_name, _), reached in zip(pending, results, strict=True)
            if not reached
        ]
        if failed:
            raise SequenceTimeoutError(
                f"シーケンス '{self.name}': 目標位置に到達しませんでした ({', '.join(failed)})"
            )

        desynced = []
        for handle, _, _ in pending:
            error = handle.sync_violation()
            if error is None:
                continue
            allowed = table.sync_tolerance(handle.name) or 0.0
            desynced.append(f"{handle.name}: 偏差 {error:.3f} > 許容 {allowed:.3f}")
        if desynced:
            raise AxisSyncError(
                f"シーケンス '{self.name}': 軸内のモータ位置がずれています ({', '.join(desynced)})"
            )

    @property
    def court(self) -> Court:
        return self._court

    def set_court(self, court: Court) -> None:
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
            if self._stop_event.is_set():
                self._current_index = 0
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
                try:
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
                    )
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
        self._last_error = None
