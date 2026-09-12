from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from lib.sequence.homing import SwitchDistance, SwitchMeasurement, measure_distances, measure_switch
from lib.server_homing import HomingSource

logger = logging.getLogger(__name__)

__all__ = ["SwitchMeasureController"]


@dataclass(frozen=True)
class _Request:
    robot: str
    axis: str
    direction: float
    step: float | None
    coarse_step: float | None
    limit: float | None


def _positive(label: str, value: object) -> tuple[float | None, str | None]:
    if value is None:
        return None, None
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None, f"{label} が数値ではありません"
    if value <= 0.0:
        return None, f"{label} は正の値で指定してください ({value})"
    return float(value), None


class SwitchMeasureController:
    """リミットスイッチの作動点 (軸と向き) と、両端のスイッチ間の距離 (ロボット単位) を実測する。

    実行は零点合わせと同じ `HomingRunner` の二段探索を通し、原点を書き込む段だけを
    行わない。対象の軸・寄せる口は零点合わせと共通 (`HomingSource`)。
    """

    def __init__(
        self,
        *,
        environment_deny: Callable[[str | None], str | None],
        seize_control: Callable[[str], Awaitable[str | None]],
        reenergize: Callable[[str], Awaitable[None]],
        broadcast: Callable[[dict], Awaitable[None]],
    ) -> None:
        self._environment_deny = environment_deny
        self._seize_control = seize_control
        self._reenergize = reenergize
        self._broadcast = broadcast

        self._source: HomingSource | None = None
        self._task: asyncio.Task[None] | None = None
        # 後始末の publish はまだタスクの中なので、task.done() では実行中に見える
        self._running = False
        self._robot: str | None = None
        self._axis: str | None = None
        self._direction: float | None = None
        self._result: SwitchMeasurement | None = None
        #: 距離測定の実行中と結果。作動点測定 (1 本) のときは None
        self._distances: list[SwitchDistance] | None = None
        self._error: str | None = None
        self._last_payload: dict | None = None

    def set_source(self, source: HomingSource) -> None:
        self._source = source

    @property
    def running(self) -> bool:
        return self._running

    def running_for(self, robot: str) -> bool:
        """その台で走っているか。台を名指しする点検どうしの排他はこれを見る。"""
        return self._running and self._robot == robot

    @property
    def error(self) -> str | None:
        return self._error

    @property
    def result(self) -> SwitchMeasurement | None:
        return self._result

    def targets(self, robot: str) -> tuple[str, ...]:
        if self._source is None:
            return ()
        return self._source.axes_by_robot.get(robot, ())

    def _read_deny(self) -> str | None:
        """台に依らない読み込み条件。"""
        if self._source is None or not self._source.axes_by_robot:
            return "作動点を測定できる軸が読み込まれていません"
        return None

    def deny_reason(self, robot: str | None = None) -> str | None:
        """可否。**台を名指しすればその台の条件で決まる** (配信は台に依らない分だけ)。"""
        read_deny = self._read_deny()
        if read_deny is not None:
            return read_deny

        environment = self._environment_deny(robot)
        if environment is not None:
            return environment

        # 状態の置き場が 1 組しかないので、走っているあいだは相手の台でも測れない
        if self.running:
            return "既に作動点測定を実行中です"
        return None

    async def start(self, data: dict) -> str | None:
        """作動点測定を開始する。**拒んだ理由を返す (通れば None)。**"""
        read_deny = self._read_deny()
        if read_deny is not None:
            return await self._reject(read_deny)

        request, reason = self._resolve(data)
        if request is None:
            return await self._reject(reason or "作動点測定の対象を決められません")

        deny = self.deny_reason(request.robot)
        if deny is not None:
            return await self._reject(deny)

        source = self._source
        assert source is not None

        self._error = None
        self._robot = request.robot
        self._axis = request.axis
        self._direction = request.direction
        # 前回の実測を残したまま走らせると、失敗しても古い値が読めてしまう
        self._result = None
        self._distances = None

        async def _run() -> None:
            try:
                # 制御権の引き取り (シーケンスが降りるのを待つ) はここ。コマンド
                # ハンドラで待つと、同じ接続の EMG STOP がそのぶん遅れる
                seized = await self._seize_control(request.robot)
                if seized is not None:
                    self._error = seized
                    return

                # 無励磁のモータを戻すのも同じ理由でここ。シーケンスが降りてからでないと
                # 再励磁の目標の捨て直しが、走っている指令とぶつかる
                await self._reenergize(request.robot)

                logger.info(
                    "作動点測定: robot=%s 軸=%s 向き=%+g",
                    request.robot,
                    request.axis,
                    request.direction,
                )
                self._result = await measure_switch(
                    source.runner,
                    source.table,
                    source.motors,
                    court=source.court(),
                    axis=request.axis,
                    direction=request.direction,
                    step=request.step,
                    coarse_step=request.coarse_step,
                    limit=request.limit,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("作動点測定に失敗: %s (%s)", request.axis, exc)
                self._error = str(exc)
            finally:
                self._running = False
                await self.publish()

        # 制御権を奪う前に running を立てる。状態の置き場は 1 組しかないので、
        # 奪っているあいだに次の測定が入るとその 1 組を取り合う
        self._running = True
        await self.publish()
        self._task = asyncio.create_task(_run())
        return None

    async def start_distance(self, data: dict) -> str | None:
        """そのロボットの全軸で距離測定を開始する。**拒んだ理由を返す (通れば None)。**"""
        read_deny = self._read_deny()
        if read_deny is not None:
            return await self._reject(read_deny)

        robot, known, reason = self._resolve_robot(data)
        if robot is None or known is None:
            return await self._reject(reason or "距離測定の対象を決められません")

        deny = self.deny_reason(robot)
        if deny is not None:
            return await self._reject(deny)

        source = self._source
        assert source is not None

        self._error = None
        self._robot = robot
        self._axis = None
        self._direction = None
        self._result = None
        self._distances = []

        async def _on_axis(axis: str) -> None:
            self._axis = axis
            await self.publish()

        async def _on_result(result: SwitchDistance) -> None:
            assert self._distances is not None
            self._distances.append(result)
            await self.publish()

        async def _run() -> None:
            try:
                # 制御権の引き取りと再励磁はここ (作動点測定と同じ理由)
                seized = await self._seize_control(robot)
                if seized is not None:
                    self._error = seized
                    return

                await self._reenergize(robot)

                logger.info("距離測定: robot=%s 軸=%s", robot, ", ".join(known))
                await measure_distances(
                    source.runner,
                    source.table,
                    source.motors,
                    court=source.court(),
                    axes=known,
                    # 寄せる口は零点合わせと同じ。前後は昇降が top に居ないと 1 歩も動けない
                    move_to=source.move_to,
                    on_axis=_on_axis,
                    on_result=_on_result,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("距離測定に失敗: %s (%s)", robot, exc)
                self._error = str(exc)
            finally:
                self._axis = None
                self._running = False
                await self.publish()

        self._running = True
        await self.publish()
        self._task = asyncio.create_task(_run())
        return None

    def _resolve_robot(self, data: dict) -> tuple[str | None, tuple[str, ...] | None, str | None]:
        robot = data.get("robot")
        if not isinstance(robot, str) or not robot:
            return None, None, "ロボットが指定されていません"

        known = self.targets(robot)
        if not known:
            return None, None, f"'{robot}' に作動点を測定できる軸がありません"
        return robot, known, None

    def _resolve(self, data: dict) -> tuple[_Request | None, str | None]:
        robot, known, reason = self._resolve_robot(data)
        if robot is None or known is None:
            return None, reason

        axis = data.get("axis")
        if not isinstance(axis, str) or not axis:
            return None, "軸が指定されていません"
        if axis not in known:
            return None, (
                f"'{axis}' は '{robot}' の測定できる軸ではありません"
                f" (指定できる軸: {', '.join(known)})"
            )

        direction = data.get("direction")
        if isinstance(direction, bool) or direction not in (1, -1, 1.0, -1.0):
            return None, "向きは +1 か -1 で指定してください"

        values: dict[str, float | None] = {}
        for key, label in (("step", "刻み"), ("coarse_step", "粗刻み"), ("limit", "移動量の上限")):
            value, reason = _positive(label, data.get(key))
            if reason is not None:
                return None, reason
            values[key] = value

        return _Request(
            robot=robot,
            axis=axis,
            direction=float(direction),
            step=values["step"],
            coarse_step=values["coarse_step"],
            limit=values["limit"],
        ), None

    async def _reject(self, reason: str) -> str:
        self._error = reason
        logger.info("作動点測定拒否: %s", reason)
        await self.publish()
        return reason

    def payload(self) -> dict:
        source = self._source
        return {
            "type": "switch_measure_state",
            "available": source is not None and bool(source.axes_by_robot),
            "blocked_reason": self.deny_reason(),
            "running": self.running,
            "robot": self._robot,
            "axis": self._axis,
            "direction": self._direction,
            "result": self._result.to_dict() if self._result is not None else None,
            "distances": None
            if self._distances is None
            else [result.to_dict() for result in self._distances],
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
