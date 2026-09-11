from __future__ import annotations

from lib.sequence.engine import Sequence, step

HOME: dict[str, str] = {
    "y_axis": "home",
    "rotate": "home",
    "gripper": "open",
    "wall_f": "initial",
    "wall_r": "initial",
    "conveyor": "stop",
}


def _pick_at(work: str) -> dict[str, str]:
    return {"y_axis": work, "rotate": "pick"}


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

    @step("初期位置へ移動")
    async def move_to_home(self) -> None:
        await self.move_to(HOME)

    @step("3 列目ワークへ移動", require_trigger=True)
    async def move_to_work_3(self) -> None:
        await self.move_to(_pick_at("work_3"))

    @step("3 列目ワークを把持", require_trigger=True)
    async def grab_work_3(self) -> None:
        await self.move_to({"gripper": "closed", "wall_f": "open"})

    @step("3 列目ワークをコンベアの位置へ", require_trigger=True)
    async def move_work_3_to_conveyor(self) -> None:
        await self.move_to({"y_axis": "work_3_after_1", "rotate": "work_3_after_1"})
        await self.move_to({"y_axis": "work_3_after_2", "rotate": "work_3_after_2"})
        await self.move_to(TO_CONVEYOR)

    @step("3 列目ワークをリリース", require_trigger=True)
    async def release_work_3(self) -> None:
        await self.move_to(RELEASE)
        await self.move_to({"rotate": "after_place", "y_axis": "clear"})

    @step("共通ワークへ移動", require_trigger=True)
    async def move_to_work_shared(self) -> None:
        await self.move_to({"y_axis": "work_shared", "rotate": "pick_shared"})

    @step("コンベアの壁を閉じてワークを寄せる")
    async def close_wall_f_3(self) -> None:
        await self.move_to(SWEEP_TO_CONVEYOR)

    @step("共通ワークを把持", require_trigger=True)
    async def grab_work_shared(self) -> None:
        await self.move_to({"gripper": "closed"})

    @step("共通ワークを引き出す", require_trigger=True)
    async def pull_out_work_shared(self) -> None:
        await self.move_to({"y_axis": "work_shared_after_1", "rotate": "work_shared_after_1"})

    @step("共通ワークを開放", require_trigger=True)
    async def release_work_shared_after_pull_out(self) -> None:
        await self.move_to({"gripper": "open"})

    @step("3 列目に置いた共通ワークへ移動", require_trigger=True)
    async def move_to_work_shared_3(self) -> None:
        await self.move_to(_pick_at("work_shared"))

    @step("3 列目に置いた共通ワークを把持", require_trigger=True)
    async def grab_work_shared_3(self) -> None:
        await self.move_to({"gripper": "closed"})

    @step("共通ワークをコンベアの位置へ", require_trigger=True)
    async def move_work_shared_to_conveyor(self) -> None:
        await self.move_to({"y_axis": "work_3_after_1", "rotate": "work_3_after_1"})
        await self.move_to({"y_axis": "work_3_after_2", "rotate": "work_3_after_2"})
        await self.move_to(TO_CONVEYOR)

    @step("共通ワークをリリース", require_trigger=True)
    async def release_work_shared(self) -> None:
        await self.move_to(RELEASE)
        await self.move_to({"rotate": "after_place", "y_axis": "clear"})

    @step("2 列目ワークへ移動")
    async def move_to_work_2(self) -> None:
        await self.move_to(_pick_at("work_2"))

    @step("コンベアの壁を閉じてワークを寄せる")
    async def close_wall_f_shared(self) -> None:
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
        await self.move_to({"rotate": "after_place", "y_axis": "clear"})

    @step("1 列目ワークへ移動", require_trigger=True)
    async def move_to_work_1(self) -> None:
        await self.move_to(_pick_at("work_1"))

    @step("コンベアの壁を閉じてワークを寄せる")
    async def close_wall_f_2(self) -> None:
        await self.move_to(SWEEP_TO_CONVEYOR)

    @step("1 列目ワークを把持", require_trigger=True)
    async def grab_work_1(self) -> None:
        await self.move_to({"gripper": "closed"})

    @step("1 列目ワークをコンベアの位置へ", require_trigger=True)
    async def move_work_1_to_conveyor(self) -> None:
        await self.move_to({"y_axis": "work_1_after_1", "rotate": "work_1_after_1"})
        await self.move_to(TO_CONVEYOR)

    @step("1 列目ワークをリリース", require_trigger=True)
    async def release_work_1(self) -> None:
        await self.move_to(RELEASE)
        await self.move_to({"rotate": "after_place", "y_axis": "clear"})

    @step("コンベアの壁を閉じてワークを寄せる")
    async def close_wall_f_1(self) -> None:
        await self.move_to(SWEEP_TO_CONVEYOR)

    @step("初期位置へ復帰")
    async def return_home(self) -> None:
        await self.move_to(HOME)
