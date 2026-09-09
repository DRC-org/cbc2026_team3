from __future__ import annotations

from lib.sequence.engine import Sequence, step

HOME: dict[str, str] = {
    "y_axis": "home",
    "rotate": "home",
    "gripper": "open",
    "wall_f": "initial",
    "wall_r": "initial",
    "conveyor": "run",
}


def _pick_at(work: str) -> tuple[dict[str, str], ...]:
    """y_axis と rotate を一息に動かすと回した機構がコンベア側・ワーク側の双方と
    干渉するため、干渉域を避けて段階的に寄せる。守っているのはこの並びだけである
    (docs/invariants.md §4)。"""
    return (
        {"y_axis": f"{work}_via_1", "rotate": f"{work}_via_1"},
        {"y_axis": f"{work}_via_2", "rotate": f"{work}_via_2"},
        {"y_axis": f"{work}_via_3", "rotate": f"{work}_via_3"},
        {"y_axis": work, "rotate": "pick"},
    )


TO_CONVEYOR: dict[str, str] = {
    "y_axis": "home",
    "rotate": "place",
    "wall_f": "open",
    "conveyor": "stop",
}

SWEEP_TO_CONVEYOR: dict[str, str] = {"wall_f": "closed", "conveyor": "run"}

RELEASE: dict[str, str] = {"gripper": "open"}


class MainHandSequence(Sequence):
    def __init__(self, name: str = "main_hand") -> None:
        super().__init__(name)

    async def _approach(self, work: str) -> None:
        for pose in _pick_at(work):
            await self.move_to(pose)

    @step("初期位置へ移動")
    async def move_to_home(self) -> None:
        await self.move_to(HOME)

    @step("自陣ワーク 3 列目まで前進", require_trigger=True)
    async def move_to_work_3(self) -> None:
        await self._approach("work_3")

    @step("自陣ワーク 3 列目を把持", require_trigger=True)
    async def grab_work_3(self) -> None:
        await self.move_to({"gripper": "closed"})

    @step("3 列目ワークをコンベアの位置へ", require_trigger=True)
    async def move_work_3_to_conveyor(self) -> None:
        await self.move_to(TO_CONVEYOR)

    @step("3 列目ワークをリリース", require_trigger=True)
    async def release_work_3(self) -> None:
        await self.move_to(RELEASE)

    @step("共通ワークへ移動", require_trigger=True)
    async def move_to_work_shared(self) -> None:
        await self._approach("work_shared")

    @step("コンベアの壁を閉じてワークを寄せる")
    async def close_wall_f_3(self) -> None:
        await self.move_to(SWEEP_TO_CONVEYOR)

    @step("共通ワークを把持", require_trigger=True)
    async def grab_work_shared(self) -> None:
        await self.move_to({"gripper": "closed"})

    @step("共通ワークをコンベアの位置へ", require_trigger=True)
    async def move_work_shared_to_conveyor(self) -> None:
        await self.move_to(TO_CONVEYOR)

    @step("共通ワークをリリース", require_trigger=True)
    async def release_work_shared(self) -> None:
        await self.move_to(RELEASE)

    @step("1 列目ワークへ移動", require_trigger=True)
    async def move_to_work_1(self) -> None:
        await self._approach("work_1")

    @step("コンベアの壁を閉じてワークを寄せる")
    async def close_wall_f_shared(self) -> None:
        await self.move_to(SWEEP_TO_CONVEYOR)

    @step("1 列目ワークを把持", require_trigger=True)
    async def grab_work_1(self) -> None:
        await self.move_to({"gripper": "closed"})

    @step("1 列目ワークをコンベアの位置へ", require_trigger=True)
    async def move_work_1_to_conveyor(self) -> None:
        await self.move_to(TO_CONVEYOR)

    @step("1 列目ワークをリリース", require_trigger=True)
    async def release_work_1(self) -> None:
        await self.move_to(RELEASE)

    @step("2 列目ワークへ移動")
    async def move_to_work_2(self) -> None:
        await self._approach("work_2")

    @step("コンベアの壁を閉じてワークを寄せる")
    async def close_wall_f_1(self) -> None:
        await self.move_to(SWEEP_TO_CONVEYOR)

    @step("2 列目ワークを把持", require_trigger=True)
    async def grab_work_2(self) -> None:
        await self.move_to({"gripper": "closed"})

    @step("2 列目ワークをコンベアの位置へ")
    async def move_work_2_to_conveyor(self) -> None:
        await self.move_to(TO_CONVEYOR)

    @step("2 列目ワークをリリース", require_trigger=True)
    async def release_work_2(self) -> None:
        await self.move_to(RELEASE)

    @step("コンベアの壁を閉じてワークを寄せる")
    async def close_wall_f_2(self) -> None:
        await self.move_to(SWEEP_TO_CONVEYOR)

    @step("初期位置へ復帰")
    async def return_home(self) -> None:
        await self.move_to(HOME)
        await self.move_to({"conveyor": "stop"})
