from __future__ import annotations

import logging
from collections.abc import Collection, Mapping

from lib.sequence.engine import Sequence, step
from lib.sequence.homing import HomingRunner, run_homing
from sequences.main_hand import HOME as MAIN_HOME
from sequences.sub_hand import VALVE_AXES

logger = logging.getLogger(__name__)


SUB_HOME: dict[str, str] = {
    "sub_y_axis": "retracted",
    "sub_lift": "top",
    "sub_rotate": "receive",
    "sub_pitch": "open",
    "sub_offset": "open",
    "pump_vac": "stop",
}


class MotorCheckSequence(Sequence):
    def __init__(
        self, name: str = "motor_check", *, available_axes: Collection[str] | None = None
    ) -> None:
        super().__init__(name)
        self._homing: HomingRunner | None = None
        if available_axes is not None:
            self.restrict_to_axes(available_axes)

    async def move_to(self, targets: Mapping[str, str], *, timeout: float | None = None) -> None:
        targets = self.available_targets(targets)
        if not targets:
            return
        await super().move_to(targets, timeout=timeout)

    def bind_homing(self, runner: HomingRunner) -> None:
        self._homing = runner

    @step("リミットスイッチで零点を確定する")
    async def home_axes(self) -> None:
        if self._homing is None:
            logger.info("零点確定: 実行口が未注入のため飛ばす")
            return

        await run_homing(self._homing, self.positions, self.motors, court=self.court)

    @step("メインハンド 初期姿勢へ", axes=MAIN_HOME.keys())
    async def main_home(self) -> None:
        await self.move_to(MAIN_HOME)

    @step("メインハンド y 軸 (左右直結ペア)", axes={"y_axis"})
    async def main_y_axis(self) -> None:
        await self.move_to({"y_axis": "work_3"})
        await self.move_to({"y_axis": "home"})

    @step("メインハンド エンドエフェクタ回転 (左右直結ペア)", axes={"rotate"})
    async def main_rotate(self) -> None:
        await self.move_to({"rotate": "pick"})
        await self.move_to({"rotate": "home"})

    @step("メインハンド グリッパ", axes={"gripper"})
    async def main_gripper(self) -> None:
        await self.move_to({"gripper": "closed"})
        await self.move_to({"gripper": "open"})

    @step("メインハンド 壁 前後", axes={"wall_f", "wall_r"})
    async def main_walls(self) -> None:
        await self.move_to({"wall_f": "closed", "wall_r": "closed"})
        await self.move_to({"wall_f": "open", "wall_r": "open"})
        await self.move_to({"wall_f": "initial", "wall_r": "initial"})

    @step("メインハンド コンベア (目視確認)", axes={"conveyor"})
    async def main_conveyor(self) -> None:
        await self.move_to({"conveyor": "run"})
        await self.move_to({"conveyor": "stop"})

    @step("サブハンド 初期姿勢へ", axes=SUB_HOME.keys())
    async def sub_home(self) -> None:
        await self.move_to(SUB_HOME)

    @step("サブハンド 前後スライド (Y 方向)", axes={"sub_y_axis"})
    async def sub_y_axis(self) -> None:
        await self.move_to({"sub_y_axis": "clear"})
        await self.move_to({"sub_y_axis": "retracted"})

    @step("サブハンド 昇降", axes={"sub_lift"})
    async def sub_lift(self) -> None:
        await self.move_to({"sub_lift": "pick"})
        await self.move_to({"sub_lift": "top"})

    @step("サブハンド 回転 (左右直結ペア)", axes={"sub_rotate"})
    async def sub_rotate(self) -> None:
        await self.move_to({"sub_rotate": "carry"})
        await self.move_to({"sub_rotate": "receive"})

    @step("サブハンド ピッチ (左右直結ペア)", axes={"sub_pitch"})
    async def sub_pitch(self) -> None:
        await self.move_to({"sub_pitch": "close"})
        await self.move_to({"sub_pitch": "open"})

    @step("サブハンド オフセット", axes={"sub_offset"})
    async def sub_offset(self) -> None:
        await self.move_to({"sub_offset": "close"})
        await self.move_to({"sub_offset": "open"})

    @step("サブハンド 電磁弁 6 個 (打音・目視確認)", axes=VALVE_AXES)
    async def sub_valves(self) -> None:
        for axis in VALVE_AXES:
            await self.move_to({axis: "open"})
            await self.move_to({axis: "closed"})

    @step("サブハンド 吸気ポンプ (聴音確認)", axes={"pump_vac"})
    async def sub_pump(self) -> None:
        await self.move_to({"pump_vac": "run"})
        await self.move_to({"pump_vac": "stop"})

    @step("両ハンドを初期姿勢へ戻す", axes={*MAIN_HOME, *SUB_HOME})
    async def restore_home(self) -> None:
        # MAIN_HOME は試合中の待機姿勢でコンベアを回したままだが、
        # 動作確認は駆動しっぱなしの軸を残さずに終える
        await self.move_to({**MAIN_HOME, **SUB_HOME, "conveyor": "stop"})
