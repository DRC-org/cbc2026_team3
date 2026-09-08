from __future__ import annotations

from lib.sequence.engine import Sequence, step

VALVE_AXES: tuple[str, ...] = ("valve_1", "valve_2", "valve_3", "valve_4", "valve_5", "valve_6")


def _all_valves(state: str) -> dict[str, str]:
    return dict.fromkeys(VALVE_AXES, state)


class SubHandSequence(Sequence):
    def __init__(self, name: str = "sub_hand") -> None:
        super().__init__(name)

    @step("初期位置へ移動")
    async def move_to_home(self) -> None:
        await self.move_to(_all_valves("closed") | {"pump_blow": "stop"})
        await self.move_to({"sub_arm_joint": "home"})
        await self.move_to({"pump_vac": "run"})

    @step("補助ハンド展開", require_trigger=True)
    async def extend_sub_arm(self) -> None:
        await self.move_to({"sub_arm_joint": "extended"})

    @step("ワーク受け取り位置へ")
    async def move_to_handoff(self) -> None:
        await self.move_to({"sub_arm_joint": "handoff"})

    @step("ワーク吸着", require_trigger=True)
    async def grip_by_suction(self) -> None:
        await self.move_to(_all_valves("open"))

    @step("配置位置へ移動", require_trigger=True)
    async def move_to_place(self) -> None:
        await self.move_to({"sub_arm_joint": "place"})

    @step("ワーク解放 (配置)", require_trigger=True)
    async def release_at_place(self) -> None:
        await self.move_to(_all_valves("closed"))
        await self.move_to({"pump_blow": "run"})
        await self.move_to({"pump_blow": "stop"})

    @step("初期位置へ復帰")
    async def return_home(self) -> None:
        await self.move_to(_all_valves("closed") | {"pump_blow": "stop"})
        await self.move_to({"sub_arm_joint": "home"})
