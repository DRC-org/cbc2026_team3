from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field

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


@dataclass
class _RobotRun:
    task: asyncio.Task[None] | None = None
    # 後始末の publish はまだタスクの中なので、task.done() では実行中に見える
    running: bool = False
    axes: tuple[str, ...] = ()
    current: str | None = None
    results: list[AxisHomingResult] = field(default_factory=list)
    error: str | None = None


class HomingController:
    """ロボットと軸を指定して零点確定だけを走らせる。

    実行は動作確認と同じ `run_homing` を通す。手順を書き写すと、片方だけが
    コートやセンサの扱いを直された状態が作れる。**「参照される軸を確定 → 寄せる →
    残りを確定」の 3 段も `run_homing` が持つ**ので、ここは寄せる口を渡すだけ。

    ロボット間は並行して走らせてよく、同じロボットへの重ね掛けだけ拒む。
    """

    def __init__(
        self,
        *,
        environment_deny: Callable[..., str | None],
        reenergize: Callable[[str], Awaitable[None]],
        broadcast: Callable[[dict], Awaitable[None]],
    ) -> None:
        self._environment_deny = environment_deny
        self._reenergize = reenergize
        self._broadcast = broadcast

        self._source: HomingSource | None = None
        self._runs: dict[str, _RobotRun] = {}
        self._last_payload: dict | None = None

    def set_source(self, source: HomingSource) -> None:
        self._source = source

    @property
    def running(self) -> bool:
        """どれか 1 機でも走っていれば True。手動操縦の制御権を塞ぐ排他はこれを見る。"""
        return any(run.running for run in self._runs.values())

    def running_for(self, robot: str) -> bool:
        run = self._runs.get(robot)
        return run is not None and run.running

    def targets(self, robot: str) -> tuple[str, ...]:
        if self._source is None:
            return ()
        return self._source.axes_by_robot.get(robot, ())

    def _run_of(self, robot: str) -> _RobotRun:
        return self._runs.setdefault(robot, _RobotRun())

    def _common_deny(self, robot: str | None = None) -> str | None:
        if self._source is None or not self._source.axes_by_robot:
            return "零点確定できる軸が読み込まれていません"
        return self._environment_deny(robot)

    def _robot_deny(self, robot: str) -> str | None:
        run = self._runs.get(robot)
        if run is not None and run.running:
            return "既にこのロボットの零点合わせを実行中です"
        return None

    def deny_reason(self, robot: str) -> str | None:
        return self._common_deny(robot) or self._robot_deny(robot)

    async def start(self, robot: str, axes: object) -> str | None:
        """零点合わせを開始する。**拒んだ理由を返す (通れば None)。**"""
        deny = self._common_deny(robot if isinstance(robot, str) else None)
        if deny is not None:
            return await self._reject(robot, deny)

        targets, reason = self._resolve(robot, axes)
        if targets is None:
            return await self._reject(robot, reason or "零点合わせの対象を決められません")

        deny = self._robot_deny(robot)
        if deny is not None:
            return await self._reject(robot, deny)

        source = self._source
        assert source is not None

        run = self._run_of(robot)
        run.error = None
        run.axes = targets
        run.current = None
        run.results = []

        async def _on_axis(axis: str) -> None:
            run.current = axis
            await self.publish()

        async def _on_result(result: AxisHomingResult) -> None:
            run.results.append(result)
            await self.publish()

        async def _run() -> None:
            logger.info("零点合わせ: robot=%s 軸=%s", robot, ", ".join(targets))
            try:
                # 無励磁のモータを戻すのはここ。コマンドハンドラで待つと、同じ接続の
                # EMG STOP がそのぶん通らない
                await self._reenergize(robot)
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
                    on_axis=_on_axis,
                    on_result=_on_result,
                    stop_on_error=False,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("零点合わせエラー: %s", exc)
                run.error = str(exc)
            finally:
                run.current = None
                run.running = False
                await self.publish()

        run.running = True
        run.task = asyncio.create_task(_run())
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

    async def _reject(self, robot: object, reason: str) -> str:
        logger.info("零点合わせ拒否: robot=%s %s", robot, reason)
        # 走っているロボットの error はその実行の結果なので上書きしない
        if isinstance(robot, str) and self.targets(robot):
            run = self._run_of(robot)
            if not run.running:
                run.error = reason
                await self.publish()
        return reason

    def payload(self) -> dict:
        source = self._source
        axes_by_robot = source.axes_by_robot if source else {}
        common = self._common_deny()
        robots = {}
        for robot in axes_by_robot:
            run = self._runs.get(robot) or _RobotRun()
            robots[robot] = {
                "blocked_reason": common or self._robot_deny(robot),
                "running": run.running,
                "axes": list(run.axes),
                "current_axis": run.current,
                "results": [{"axis": r.axis, "error": r.error} for r in run.results],
                "error": run.error,
            }
        return {
            "type": "homing_state",
            "available": bool(axes_by_robot),
            "running": self.running,
            "targets": {robot: list(axes) for robot, axes in axes_by_robot.items()},
            "robots": robots,
        }

    async def publish(self) -> None:
        payload = self.payload()
        if payload == self._last_payload:
            return
        self._last_payload = payload
        await self._broadcast(payload)
