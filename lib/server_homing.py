from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

from lib.match_state import Court
from lib.sequence.homing import AxisHomingResult, HomingRunner, MoveTo, run_homing
from lib.sequence.motors import MotorGroup
from lib.sequence.positions import PositionTable

logger = logging.getLogger(__name__)

__all__ = ["HomingController", "HomingSource"]


@dataclass(frozen=True)
class HomingSource:
    runner: HomingRunner
    table: PositionTable
    motors: MotorGroup
    #: 毎回問い直す。コートで探索の向きが鏡になるので、起動時に固めると切り替えに追従しない
    court: Callable[[], Court | None]
    axes_by_robot: Mapping[str, tuple[str, ...]]
    #: 位置名で軸を寄せる口。**`guard.requires` の参照先が今回選ばれていれば**、
    #: 零点確定の前にそこへ寄せる (`run_homing`)。配線しないと寄せずに拒否させる
    move_to: MoveTo | None = None


class HomingController:
    """ロボットと軸を指定して零点確定だけを走らせる。

    実行は動作確認と同じ `run_homing` を通す。手順を書き写すと、片方だけが
    コートやセンサの扱いを直された状態が作れる。**「参照される軸を確定 → 寄せる →
    残りを確定」の 3 段も `run_homing` が持つ**ので、ここは寄せる口を渡すだけ。
    """

    def __init__(
        self,
        *,
        environment_deny: Callable[[], str | None],
        broadcast: Callable[[dict], Awaitable[None]],
    ) -> None:
        self._environment_deny = environment_deny
        self._broadcast = broadcast

        self._source: HomingSource | None = None
        self._task: asyncio.Task[None] | None = None
        # 後始末の publish はまだタスクの中なので、task.done() では実行中に見える
        self._running = False
        self._robot: str | None = None
        self._axes: tuple[str, ...] = ()
        self._current: str | None = None
        self._results: list[AxisHomingResult] = []
        self._error: str | None = None
        self._last_payload: dict | None = None

    def set_source(self, source: HomingSource) -> None:
        self._source = source

    @property
    def running(self) -> bool:
        return self._running

    @property
    def error(self) -> str | None:
        return self._error

    def targets(self, robot: str) -> tuple[str, ...]:
        if self._source is None:
            return ()
        return self._source.axes_by_robot.get(robot, ())

    def deny_reason(self) -> str | None:
        if self._source is None or not self._source.axes_by_robot:
            return "零点確定できる軸が読み込まれていません"

        environment = self._environment_deny()
        if environment is not None:
            return environment

        if self.running:
            return "既に零点合わせを実行中です"
        return None

    async def start(self, robot: str, axes: object) -> str | None:
        """零点合わせを開始する。**拒んだ理由を返す (通れば None)。**"""
        deny = self.deny_reason()
        if deny is not None:
            return await self._reject(deny)

        targets, reason = self._resolve(robot, axes)
        if targets is None:
            return await self._reject(reason or "零点合わせの対象を決められません")

        source = self._source
        assert source is not None

        self._error = None
        self._robot = robot
        self._axes = targets
        self._current = None
        self._results = []

        async def _run() -> None:
            logger.info("零点合わせ: robot=%s 軸=%s", robot, ", ".join(targets))
            try:
                await run_homing(
                    source.runner,
                    source.table,
                    source.motors,
                    court=source.court(),
                    axes=targets,
                    # 寄せるのは選ばれた軸だけ。昇降を選ばずに前後だけ選んだら
                    # 寄せずに拒否させ、文面で手当てを案内する (`run_homing` の
                    # docstring。この非対称が仕様)
                    move_to=source.move_to,
                    on_axis=self._on_axis,
                    on_result=self._on_result,
                    stop_on_error=False,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("零点合わせエラー: %s", exc)
                self._error = str(exc)
            finally:
                self._current = None
                self._running = False
                await self.publish()

        self._running = True
        self._task = asyncio.create_task(_run())
        await self.publish()
        return None

    def _resolve(self, robot: object, axes: object) -> tuple[tuple[str, ...] | None, str | None]:
        if not isinstance(robot, str) or not robot:
            return None, "ロボットが指定されていません"

        known = self.targets(robot)
        if not known:
            return None, f"'{robot}' に零点確定できる軸がありません"

        if axes is None:
            return known, None
        if not isinstance(axes, list) or not axes or not all(isinstance(a, str) for a in axes):
            return None, "軸の指定が軸名の配列ではありません"

        unknown = [axis for axis in axes if axis not in known]
        if unknown:
            return None, (
                f"{', '.join(unknown)} は '{robot}' の零点確定できる軸ではありません"
                f" (指定できる軸: {', '.join(known)})"
            )
        return tuple(axes), None

    async def _on_axis(self, axis: str) -> None:
        self._current = axis
        await self.publish()

    async def _on_result(self, result: AxisHomingResult) -> None:
        self._results.append(result)
        await self.publish()

    async def _reject(self, reason: str) -> str:
        self._error = reason
        logger.info("零点合わせ拒否: %s", reason)
        await self.publish()
        return reason

    def payload(self) -> dict:
        source = self._source
        return {
            "type": "homing_state",
            "available": source is not None and bool(source.axes_by_robot),
            "blocked_reason": self.deny_reason(),
            "running": self.running,
            "robot": self._robot,
            "axes": list(self._axes),
            "current_axis": self._current,
            "results": [{"axis": result.axis, "error": result.error} for result in self._results],
            "error": self._error,
            "targets": {
                robot: list(axes)
                for robot, axes in (source.axes_by_robot.items() if source else ())
            },
        }

    async def publish(self) -> None:
        payload = self.payload()
        if payload == self._last_payload:
            return
        self._last_payload = payload
        await self._broadcast(payload)
