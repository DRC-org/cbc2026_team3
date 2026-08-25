from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from lib.match_state import Court
from lib.sequence.positions import PositionTable

if TYPE_CHECKING:
    from lib.sequence.motors import MotorGroup

logger = logging.getLogger(__name__)


class SequenceTimeoutError(RuntimeError):
    """目標位置に到達しないままタイムアウトしたときに送出される。

    run() が例外を捕まえてシーケンスを停止させるため、掴めていないワークを
    搬送するといった「黙って次のステップへ進む」事故を防げる。
    """


@dataclass
class StepInfo:
    label: str
    method_name: str
    require_trigger: bool
    # 全自動モードでもトリガー待ちを維持するか。人間の目視確認が必須な危険動作に付ける
    auto_stop: bool = False


def step(label: str, *, require_trigger: bool = False, auto_stop: bool = False) -> Callable:
    def decorator(method: Callable) -> Callable:
        method._step_label = label  # type: ignore[attr-defined]
        method._step_require_trigger = require_trigger  # type: ignore[attr-defined]
        method._step_auto_stop = auto_stop  # type: ignore[attr-defined]
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
                        auto_stop=getattr(value, "_step_auto_stop", False),
                    )
                )
        cls._steps = steps

    def __init__(self, name: str) -> None:
        self.name = name
        self._current_index: int = 0
        self._waiting_trigger: bool = False
        self._running: bool = False
        self._trigger_event: asyncio.Event = asyncio.Event()
        # request_stop で run() ループを抜けさせるイベント
        self._stop_event: asyncio.Event = asyncio.Event()
        # 通常停止・完走後に外部から再開要求を受けるためのイベント
        self._resume_event: asyncio.Event = asyncio.Event()
        # request_jump で次の反復に反映する目標 index
        self._jump_request: int | None = None
        self._on_step_change: Callable[[dict], None] | None = None
        # 自陣コート。赤青で配置が左右反転するため各 step 内で参照して動作を分ける
        self._court: Court = Court.RED
        # 全自動モード。True のとき require_trigger のステップを待たずに通過する
        self._auto_advance: bool = False
        # モータアクセス層。bind_motors で外部から注入する (未注入でもシーケンスは動作する)
        self._motors: MotorGroup | None = None
        # 機構位置の定数表。bind_positions で外部から注入する (未注入でもシーケンスは動作する)
        self._positions: PositionTable | None = None

    # ------------------------------------------------------------------ #
    #  モータアクセス
    # ------------------------------------------------------------------ #

    def bind_motors(self, group: MotorGroup) -> None:
        self._motors = group

    @property
    def has_motors(self) -> bool:
        return self._motors is not None

    @property
    def motors(self) -> MotorGroup:
        # 未 bind でもシーケンス自体は動かせる必要があるため、参照された時点で初めて弾く
        if self._motors is None:
            raise RuntimeError(
                f"シーケンス '{self.name}' に MotorGroup が bind されていません "
                "(bind_motors を呼んでください)"
            )
        return self._motors

    async def wait_all_reached(
        self,
        *,
        tolerance: float | None = None,
        timeout: float | None = None,
    ) -> bool:
        return await self.motors.wait_all_reached(tolerance=tolerance, timeout=timeout)

    # ------------------------------------------------------------------ #
    #  機構位置の定数
    # ------------------------------------------------------------------ #

    def bind_positions(self, table: PositionTable) -> None:
        self._positions = table

    @property
    def has_positions(self) -> bool:
        return self._positions is not None

    @property
    def positions(self) -> PositionTable:
        # bind_motors と同じ方針。未 bind でもシーケンス定義自体は成立させる
        if self._positions is None:
            raise RuntimeError(
                f"シーケンス '{self.name}' に PositionTable が bind されていません "
                "(bind_positions を呼んでください)"
            )
        return self._positions

    async def move_to(
        self,
        targets: Mapping[str, str],
        *,
        timeout: float | None = None,
    ) -> None:
        """``{軸名: 位置名}`` を位置定数から引いて指令し、全軸の到達を待つ。

        単位換算・許容差・待ち時間はすべて位置定数 yaml の責務なので、
        シーケンス本体には生の数値が現れない。到達しない軸があれば
        SequenceTimeoutError を送出して停止させる。
        指令値は保持したままにする (落下すると危険な軸で保持トルクを失わないため)。
        """
        table = self.positions
        pending: list[tuple[str, str, Awaitable[bool]]] = []

        for axis, position_name in targets.items():
            handle = getattr(self.motors, axis)
            value = table.command(axis, position_name, court=self.court)
            await handle.set_position(value)
            pending.append(
                (
                    axis,
                    position_name,
                    handle.wait_reached(
                        tolerance=table.tolerance(axis),
                        timeout=table.timeout(axis) if timeout is None else timeout,
                    ),
                )
            )

        results = await asyncio.gather(*(awaitable for _, _, awaitable in pending))
        failed = [
            f"{axis}->{position_name}"
            for (axis, position_name, _), reached in zip(pending, results, strict=True)
            if not reached
        ]
        if failed:
            raise SequenceTimeoutError(
                f"シーケンス '{self.name}': 目標位置に到達しませんでした ({', '.join(failed)})"
            )

    @property
    def court(self) -> Court:
        return self._court

    def set_court(self, court: Court) -> None:
        self._court = court

    @property
    def auto_advance(self) -> bool:
        return self._auto_advance

    def set_auto_advance(self, enabled: bool) -> None:
        self._auto_advance = bool(enabled)

    @property
    def current_step(self) -> StepInfo | None:
        if 0 <= self._current_index < len(self._steps):
            return self._steps[self._current_index]
        return None

    @property
    def waiting_trigger(self) -> bool:
        return self._waiting_trigger

    @property
    def steps_info(self) -> list[dict]:
        return [
            {
                "index": i,
                "label": s.label,
                "require_trigger": s.require_trigger,
                "auto_stop": s.auto_stop,
            }
            for i, s in enumerate(self._steps)
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
        }

    def trigger(self) -> None:
        if self._waiting_trigger:
            self._trigger_event.set()

    def request_jump(self, index: int) -> None:
        """指定インデックスへジャンプ。実行中なら次の境界で反映、停止中なら再開。"""
        if not (0 <= index < len(self._steps)):
            return
        self._jump_request = index
        if self._running:
            # 実行中: トリガー待機を解除してジャンプを反映させる
            self._trigger_event.set()
        else:
            # 通常停止後・完走後: 再開イベントで run() ループを起こす
            self._resume_event.set()

    def request_stop(self) -> None:
        """通常停止 (緊急停止と異なり CAN 層には介入しない)。"""
        self._stop_event.set()
        if self._waiting_trigger:
            self._trigger_event.set()

    def request_start(self) -> None:
        """先頭から実行開始。完走後・停止後の再起動に使う。"""
        self._jump_request = 0
        self._resume_event.set()

    async def run(self) -> None:
        self._running = True
        self._stop_event.clear()
        try:
            while not self._stop_event.is_set():
                if self._jump_request is not None:
                    self._current_index = self._jump_request
                    self._jump_request = None
                if self._current_index >= len(self._steps):
                    break
                step_info = self._steps[self._current_index]
                self._notify_step_change()

                # 全自動でもトリガーを要するのは auto_stop 指定のステップのみ
                needs_trigger = step_info.require_trigger and (
                    not self._auto_advance or step_info.auto_stop
                )
                if needs_trigger:
                    self._waiting_trigger = True
                    self._trigger_event.clear()
                    await self._trigger_event.wait()
                    self._waiting_trigger = False
                    if self._stop_event.is_set():
                        break
                    if self._jump_request is not None:
                        continue

                method = getattr(self, step_info.method_name)
                try:
                    await method()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "シーケンス '%s' のステップ '%s' で例外", self.name, step_info.label
                    )
                    break

                if self._jump_request is None:
                    self._current_index += 1
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

    def set_on_step_change(self, callback: Callable[[dict], None]) -> None:
        self._on_step_change = callback

    def _notify_step_change(self) -> None:
        if self._on_step_change is not None:
            self._on_step_change(self.progress)
