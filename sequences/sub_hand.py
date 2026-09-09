from __future__ import annotations

from lib.sequence.engine import Sequence, step
from lib.suction import SuctionSelection, SuctionSelectionError

VALVE_AXES: tuple[str, ...] = ("valve_1", "valve_2", "valve_3", "valve_4", "valve_5", "valve_6")


def _all_valves(state: str) -> dict[str, str]:
    return dict.fromkeys(VALVE_AXES, state)


class SubHandSequence(Sequence):
    def __init__(self, name: str = "sub_hand", suction: SuctionSelection | None = None) -> None:
        super().__init__(name)
        self.suction = suction if suction is not None else SuctionSelection.numbered(VALVE_AXES)

    @step("初期位置へ移動")
    async def move_to_home(self) -> None:
        await self.move_to(_all_valves("closed") | {"pump_blow": "stop"})
        await self.move_to({"pump_vac": "run"})

    @step("ワーク吸着", require_trigger=True)
    async def grip_by_suction(self) -> None:
        enabled = self.suction.enabled()
        if not enabled:
            raise SuctionSelectionError(
                "吸着に使うパッドが 1 つも選ばれていません (吸着パッドの選択を確認してください)"
            )
        # 選ばれていない弁は閉じ直す。開いたままの弁が 1 つあると真空が抜ける
        await self.move_to(_all_valves("closed") | dict.fromkeys(enabled, "open"))

    @step("ワーク解放 (配置)", require_trigger=True)
    async def release_at_place(self) -> None:
        await self.move_to(_all_valves("closed"))
        await self.move_to({"pump_blow": "run"})
        await self.move_to({"pump_blow": "stop"})

    @step("初期位置へ復帰")
    async def return_home(self) -> None:
        await self.move_to(_all_valves("closed") | {"pump_blow": "stop"})
