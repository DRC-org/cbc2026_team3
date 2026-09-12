from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

from lib.sequence.motors import origin_confirmed
from lib.sequence.positions import PositionLookupError
from lib.server_homing import HomingSource

logger = logging.getLogger(__name__)

__all__ = ["ReturnHomeController"]

#: 1 通ずつ、この順で投げる位置名の組。並びは呼び出し側 (シーケンス) が持つ
type Poses = tuple[Mapping[str, str], ...]


@dataclass
class _RobotRun:
    task: asyncio.Task[None] | None = None
    # 後始末の publish はまだタスクの中なので、task.done() では実行中に見える
    running: bool = False
    abort_requested: bool = False
    #: 実行中の手順 (1 起点)。走っていなければ None
    index: int | None = None
    completed: bool | None = None
    error: str | None = None


class ReturnHomeController:
    """ロボットを試合シーケンスの初期位置へ戻す。

    投げる列を持つのはシーケンス側 (`sequences.initial_poses`)、実際に動かすのは
    動作確認・零点合わせと同じ `HomingSource.move_to`。ここはロボットを選んで
    順に渡すだけで、干渉判定・可動端保護・到達判定は全てその口が持つ。

    ロボット間は並行して走らせてよく、同じロボットへの重ね掛けだけ拒む。
    """

    def __init__(
        self,
        *,
        environment_deny: Callable[[str], str | None],
        seize_control: Callable[[str], Awaitable[str | None]],
        is_e_stop_active: Callable[[], bool],
        broadcast: Callable[[dict], Awaitable[None]],
    ) -> None:
        self._environment_deny = environment_deny
        self._seize_control = seize_control
        self._is_e_stop_active = is_e_stop_active
        self._broadcast = broadcast

        self._source: HomingSource | None = None
        self._poses: dict[str, Poses] = {}
        self._runs: dict[str, _RobotRun] = {}
        self._last_payload: dict | None = None

    def set_source(self, source: HomingSource) -> None:
        self._source = source

    def set_poses(self, poses: Mapping[str, Poses]) -> None:
        self._poses = dict(poses)

    @property
    def running(self) -> bool:
        """どれか 1 機でも走っていれば True。手動操縦の制御権を塞ぐ排他はこれを見る。"""
        return any(run.running for run in self._runs.values())

    def running_for(self, robot: str) -> bool:
        """その台で走っているか。台を名指しする点検どうしの排他はこれを見る。"""
        run = self._runs.get(robot)
        return run is not None and run.running

    def _run_of(self, robot: str) -> _RobotRun:
        return self._runs.setdefault(robot, _RobotRun())

    def _read_deny(self) -> str | None:
        """台に依らない読み込み条件。"""
        if self._source is None:
            return "初期位置へ戻す口が読み込まれていません"
        if not self._poses:
            return "初期位置が定義されているロボットがありません"
        return None

    def _robot_deny(self, robot: str) -> str | None:
        """その台の環境条件・重ね掛け・零点。**可否は台ごとに決まる** (相手の台は見ない)。"""
        deny = self._environment_deny(robot)
        if deny is not None:
            return deny

        if self.running_for(robot):
            return "既にこのロボットの原点復帰を実行中です"

        unconfirmed = self._unconfirmed_axes(robot)
        if unconfirmed:
            return (
                f"軸 {', '.join(unconfirmed)} の零点が確定していないため初期位置へ戻せません"
                " (原点の決まっていない軸へ位置名で指令すると、どこへ動くか分かりません)。"
                "先にその軸の零点確定を済ませてください"
            )
        return None

    def _unconfirmed_axes(self, robot: str) -> tuple[str, ...]:
        """投げる列に載っていて、零点が未確定の軸。**確定の正は `origin_confirmed`。**"""
        source = self._source
        if source is None:
            return ()

        found: list[str] = []
        for pose in self._poses.get(robot, ()):
            for axis in pose:
                if axis in found:
                    continue
                try:
                    spec = source.table.axis(axis)
                except PositionLookupError:
                    # この構成に居ない軸。`move_to` 側も同じ理由で黙って落とす
                    continue
                # `homing:` を持たない軸 (弁・ポンプ・サーボ) は確定という状態を持たない
                if spec.homing is None:
                    continue
                if not origin_confirmed(spec, source.motors):
                    found.append(axis)
        return tuple(found)

    def deny_reason(self, robot: str) -> str | None:
        return self._read_deny() or self._robot_deny(robot)

    async def start(self, robot: object) -> str | None:
        """原点復帰を開始する。**拒んだ理由を返す (通れば None)。**"""
        deny = self._read_deny()
        if deny is not None:
            return await self._reject(robot, deny)

        if not isinstance(robot, str) or not robot:
            return await self._reject(robot, "ロボットが指定されていません")
        if robot not in self._poses:
            return await self._reject(robot, f"'{robot}' に戻せる初期位置がありません")

        deny = self._robot_deny(robot)
        if deny is not None:
            return await self._reject(robot, deny)

        source = self._source
        assert source is not None
        move_to = source.move_to
        if move_to is None:
            return await self._reject(robot, "位置名で軸を寄せる口が配線されていません")

        poses = self._poses[robot]
        run = self._run_of(robot)
        run.abort_requested = False
        run.error = None
        run.completed = None
        run.index = None

        async def _run() -> None:
            try:
                # 制御権の引き取り (シーケンスが降りるのを待つ) はここ。コマンド
                # ハンドラで待つと、同じ接続の EMG STOP がそのぶん遅れる
                seized = await self._seize_control(robot)
                if seized is not None:
                    run.error = seized
                    return

                logger.info("原点復帰: robot=%s 手順=%d", robot, len(poses))
                for index, pose in enumerate(poses, start=1):
                    # 緊急停止と中断はここで見る。指令の入口も無励磁を拒むが、
                    # 走り終えるのを待ってから気付くより 1 手順ぶん早く止まる
                    if self._is_e_stop_active():
                        run.error = "緊急停止が入ったため原点復帰を中断しました"
                        break
                    if run.abort_requested:
                        run.error = "原点復帰を中断しました"
                        break
                    run.index = index
                    await self.publish()
                    await move_to(pose)
            except asyncio.CancelledError:
                run.error = "原点復帰を中断しました"
                raise
            except Exception as exc:
                # **並びは干渉制約そのものなので、失敗した手順の続きは投げない。**
                # 零点確定 (軸ごとに独立) と違い、次の手順は前の姿勢を前提にしている
                logger.exception("原点復帰エラー: robot=%s %s", robot, exc)
                run.error = str(exc)
            finally:
                run.index = None
                run.running = False
                run.completed = run.error is None
                await self.publish()

        # 制御権を奪う前に running を立てる。奪っているあいだに二重押しや
        # 別の点検が割り込むと、同じ軸にタスクが 2 つ走る
        run.running = True
        await self.publish()
        run.task = asyncio.create_task(_run())
        return None

    def abort(self) -> None:
        """走っている全機を次の手順の境目で止める。**今出ている 1 通は取り消せない。**"""
        for run in self._runs.values():
            run.abort_requested = True

    async def _reject(self, robot: object, reason: str) -> str:
        logger.info("原点復帰拒否: robot=%s %s", robot, reason)
        # 走っているロボットの error はその実行の結果なので上書きしない
        if isinstance(robot, str) and robot in self._poses:
            run = self._run_of(robot)
            if not run.running:
                run.error = reason
                await self.publish()
        return reason

    def payload(self) -> dict:
        read_deny = self._read_deny()
        robots = {}
        for robot, poses in self._poses.items():
            run = self._runs.get(robot) or _RobotRun()
            robots[robot] = {
                "blocked_reason": read_deny or self._robot_deny(robot),
                "running": run.running,
                "steps": len(poses),
                "current_step": run.index,
                "completed": run.completed,
                "error": run.error,
            }
        return {
            "type": "return_home_state",
            "available": self._source is not None and bool(self._poses),
            "running": self.running,
            "robots": robots,
        }

    async def publish(self) -> None:
        payload = self.payload()
        if payload == self._last_payload:
            return
        self._last_payload = payload
        await self._broadcast(payload)
