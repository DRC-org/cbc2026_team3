from __future__ import annotations

from lib.sequence.engine import Sequence, step
from lib.suction import SuctionSelection, SuctionSelectionError

VALVE_AXES: tuple[str, ...] = ("valve_1", "valve_2", "valve_3", "valve_4", "valve_5", "valve_6")

# 前後に動かしてよいのは sub_lift が top のときだけ、sub_rotate を回してよいのは
# sub_y_axis が clear に居るときだけ。条件そのものは config/sub_hand_positions.yaml の
# guard.requires が宣言し、指令の入口が判定する (docs/invariants.md §4)。この並びは
# 「拒否されずに通る唯一の順序」であって、守りの最後の 1 枚ではない。
TO_SHELF: dict[str, str] = {"sub_y_axis": "receive"}
TO_CLEAR: dict[str, str] = {"sub_y_axis": "clear"}
TO_RETRACTED: dict[str, str] = {"sub_y_axis": "retracted"}

DOWN_TO_PICK: dict[str, str] = {"sub_lift": "pick"}
DOWN_TO_PLACE: dict[str, str] = {"sub_lift": "place"}
UP_TO_TOP: dict[str, str] = {"sub_lift": "top"}

CARRY_POSE: dict[str, str] = {"sub_rotate": "carry"}
RECEIVE_POSE: dict[str, str] = {"sub_rotate": "receive"}

# ピッチとオフセットは同時に動かさない。閉じるとき オフセット -> ピッチ、
# 開くとき ピッチ -> オフセット。
CLOSE_OFFSET: dict[str, str] = {"sub_offset": "close"}
CLOSE_PITCH: dict[str, str] = {"sub_pitch": "close"}
OPEN_PITCH: dict[str, str] = {"sub_pitch": "open"}
OPEN_OFFSET: dict[str, str] = {"sub_offset": "open"}

PUMP_RUN: dict[str, str] = {"pump_vac": "run"}


def _all_valves(state: str) -> dict[str, str]:
    return dict.fromkeys(VALVE_AXES, state)


class SubHandSequence(Sequence):
    def __init__(self, name: str = "sub_hand", suction: SuctionSelection | None = None) -> None:
        super().__init__(name)
        self.suction = suction if suction is not None else SuctionSelection.numbered(VALVE_AXES)

    async def _grip_by_suction(self) -> None:
        enabled = self.suction.enabled()
        if not enabled:
            raise SuctionSelectionError(
                "吸着に使うパッドが 1 つも選ばれていません (吸着パッドの選択を確認してください)"
            )
        # 選ばれていない弁は閉じ直す。開いたままの弁が 1 つあると真空が抜ける
        await self.move_to(_all_valves("closed") | dict.fromkeys(enabled, "open"))

    async def _release(self) -> None:
        # 三方弁は閉じた側がパッドを大気開放するので、閉じるだけで残圧が抜けてワークが離れる
        await self.move_to(_all_valves("closed"))

    @step("初期位置へ移動")
    async def move_to_initial(self) -> None:
        await self.move_to(_all_valves("closed") | PUMP_RUN)
        # 昇降を上げてから前後、後退しきってから回転の順に戻す。どの姿勢から
        # 押されても干渉制約を踏まないのはこの順序だけである
        await self.move_to(UP_TO_TOP)
        await self.move_to(TO_RETRACTED)
        await self.move_to(OPEN_PITCH)
        await self.move_to(OPEN_OFFSET)
        await self.move_to(RECEIVE_POSE)

    @step("1 個目: 棚へ寄せる", require_trigger=True)
    async def work_1_to_shelf(self) -> None:
        await self.move_to(TO_SHELF)

    @step("1 個目: 吸着高さへ下降")
    async def work_1_down_to_pick(self) -> None:
        await self.move_to(DOWN_TO_PICK)

    @step("1 個目: ワーク吸着", require_trigger=True)
    async def work_1_grip(self) -> None:
        await self._grip_by_suction()

    @step("1 個目: 持ち上げ")
    async def work_1_lift_up(self) -> None:
        await self.move_to(UP_TO_TOP)

    @step("1 個目: 回転可能位置へ後退")
    async def work_1_clear_before_turn(self) -> None:
        await self.move_to(TO_CLEAR)

    @step("1 個目: 搬送姿勢へ")
    async def work_1_carry_pose(self) -> None:
        await self.move_to(CARRY_POSE)

    @step("1 個目: 箱 1 の上へ")
    async def work_1_over_box(self) -> None:
        await self.move_to({"sub_y_axis": "place_1"})

    @step("1 個目: オフセットを閉じる")
    async def work_1_close_offset(self) -> None:
        await self.move_to(CLOSE_OFFSET)

    @step("1 個目: ピッチを閉じる")
    async def work_1_close_pitch(self) -> None:
        await self.move_to(CLOSE_PITCH)

    @step("1 個目: 箱へ下降", require_trigger=True)
    async def work_1_down_to_place(self) -> None:
        await self.move_to(DOWN_TO_PLACE)

    @step("1 個目: ワーク解放 (配置)", require_trigger=True)
    async def work_1_release(self) -> None:
        await self._release()

    @step("1 個目: 上昇")
    async def work_1_up_to_top(self) -> None:
        await self.move_to(UP_TO_TOP)

    @step("1 個目: ピッチを開く")
    async def work_1_open_pitch(self) -> None:
        await self.move_to(OPEN_PITCH)

    @step("1 個目: オフセットを開く")
    async def work_1_open_offset(self) -> None:
        await self.move_to(OPEN_OFFSET)

    @step("1 個目: 回転可能位置へ後退")
    async def work_1_clear_after_place(self) -> None:
        await self.move_to(TO_CLEAR)

    @step("1 個目: 受け取り姿勢へ")
    async def work_1_receive_pose(self) -> None:
        await self.move_to(RECEIVE_POSE)

    @step("2 個目: 棚へ寄せる", require_trigger=True)
    async def work_2_to_shelf(self) -> None:
        await self.move_to(TO_SHELF)

    @step("2 個目: 吸着高さへ下降")
    async def work_2_down_to_pick(self) -> None:
        await self.move_to(DOWN_TO_PICK)

    @step("2 個目: ワーク吸着", require_trigger=True)
    async def work_2_grip(self) -> None:
        await self._grip_by_suction()

    @step("2 個目: 持ち上げ")
    async def work_2_lift_up(self) -> None:
        await self.move_to(UP_TO_TOP)

    @step("2 個目: 回転可能位置へ後退")
    async def work_2_clear_before_turn(self) -> None:
        await self.move_to(TO_CLEAR)

    @step("2 個目: 搬送姿勢へ")
    async def work_2_carry_pose(self) -> None:
        await self.move_to(CARRY_POSE)

    @step("2 個目: 箱 2 の上へ")
    async def work_2_over_box(self) -> None:
        await self.move_to({"sub_y_axis": "place_2"})

    @step("2 個目: オフセットを閉じる")
    async def work_2_close_offset(self) -> None:
        await self.move_to(CLOSE_OFFSET)

    @step("2 個目: ピッチを閉じる")
    async def work_2_close_pitch(self) -> None:
        await self.move_to(CLOSE_PITCH)

    @step("2 個目: 箱へ下降", require_trigger=True)
    async def work_2_down_to_place(self) -> None:
        await self.move_to(DOWN_TO_PLACE)

    @step("2 個目: ワーク解放 (配置)", require_trigger=True)
    async def work_2_release(self) -> None:
        await self._release()

    @step("2 個目: 上昇")
    async def work_2_up_to_top(self) -> None:
        await self.move_to(UP_TO_TOP)

    @step("2 個目: ピッチを開く")
    async def work_2_open_pitch(self) -> None:
        await self.move_to(OPEN_PITCH)

    @step("2 個目: オフセットを開く")
    async def work_2_open_offset(self) -> None:
        await self.move_to(OPEN_OFFSET)

    @step("2 個目: 回転可能位置へ後退")
    async def work_2_clear_after_place(self) -> None:
        await self.move_to(TO_CLEAR)

    @step("2 個目: 受け取り姿勢へ")
    async def work_2_receive_pose(self) -> None:
        await self.move_to(RECEIVE_POSE)

    @step("3 個目: 棚へ寄せる", require_trigger=True)
    async def work_3_to_shelf(self) -> None:
        await self.move_to(TO_SHELF)

    @step("3 個目: 吸着高さへ下降")
    async def work_3_down_to_pick(self) -> None:
        await self.move_to(DOWN_TO_PICK)

    @step("3 個目: ワーク吸着", require_trigger=True)
    async def work_3_grip(self) -> None:
        await self._grip_by_suction()

    @step("3 個目: 持ち上げ")
    async def work_3_lift_up(self) -> None:
        await self.move_to(UP_TO_TOP)

    @step("3 個目: 回転可能位置へ後退")
    async def work_3_clear_before_turn(self) -> None:
        await self.move_to(TO_CLEAR)

    @step("3 個目: 搬送姿勢へ")
    async def work_3_carry_pose(self) -> None:
        await self.move_to(CARRY_POSE)

    @step("3 個目: 箱 3 の上へ")
    async def work_3_over_box(self) -> None:
        await self.move_to({"sub_y_axis": "place_3"})

    @step("3 個目: オフセットを閉じる")
    async def work_3_close_offset(self) -> None:
        await self.move_to(CLOSE_OFFSET)

    @step("3 個目: ピッチを閉じる")
    async def work_3_close_pitch(self) -> None:
        await self.move_to(CLOSE_PITCH)

    @step("3 個目: 箱へ下降", require_trigger=True)
    async def work_3_down_to_place(self) -> None:
        await self.move_to(DOWN_TO_PLACE)

    @step("3 個目: ワーク解放 (配置)", require_trigger=True)
    async def work_3_release(self) -> None:
        await self._release()

    @step("3 個目: 上昇")
    async def work_3_up_to_top(self) -> None:
        await self.move_to(UP_TO_TOP)

    @step("3 個目: ピッチを開く")
    async def work_3_open_pitch(self) -> None:
        await self.move_to(OPEN_PITCH)

    @step("3 個目: オフセットを開く")
    async def work_3_open_offset(self) -> None:
        await self.move_to(OPEN_OFFSET)

    @step("3 個目: 回転可能位置へ後退")
    async def work_3_clear_after_place(self) -> None:
        await self.move_to(TO_CLEAR)

    @step("3 個目: 受け取り姿勢へ")
    async def work_3_receive_pose(self) -> None:
        await self.move_to(RECEIVE_POSE)

    @step("4 個目: 棚へ寄せる", require_trigger=True)
    async def work_4_to_shelf(self) -> None:
        await self.move_to(TO_SHELF)

    @step("4 個目: 吸着高さへ下降")
    async def work_4_down_to_pick(self) -> None:
        await self.move_to(DOWN_TO_PICK)

    @step("4 個目: ワーク吸着", require_trigger=True)
    async def work_4_grip(self) -> None:
        await self._grip_by_suction()

    @step("4 個目: 持ち上げ")
    async def work_4_lift_up(self) -> None:
        await self.move_to(UP_TO_TOP)

    @step("4 個目: 回転可能位置へ後退")
    async def work_4_clear_before_turn(self) -> None:
        await self.move_to(TO_CLEAR)

    @step("4 個目: 搬送姿勢へ")
    async def work_4_carry_pose(self) -> None:
        await self.move_to(CARRY_POSE)

    @step("4 個目: 箱 4 の上へ")
    async def work_4_over_box(self) -> None:
        await self.move_to({"sub_y_axis": "place_4"})

    @step("4 個目: オフセットを閉じる")
    async def work_4_close_offset(self) -> None:
        await self.move_to(CLOSE_OFFSET)

    @step("4 個目: ピッチを閉じる")
    async def work_4_close_pitch(self) -> None:
        await self.move_to(CLOSE_PITCH)

    @step("4 個目: 箱へ下降", require_trigger=True)
    async def work_4_down_to_place(self) -> None:
        await self.move_to(DOWN_TO_PLACE)

    @step("4 個目: ワーク解放 (配置)", require_trigger=True)
    async def work_4_release(self) -> None:
        await self._release()

    @step("4 個目: 上昇")
    async def work_4_up_to_top(self) -> None:
        await self.move_to(UP_TO_TOP)

    @step("4 個目: ピッチを開く")
    async def work_4_open_pitch(self) -> None:
        await self.move_to(OPEN_PITCH)

    @step("4 個目: オフセットを開く")
    async def work_4_open_offset(self) -> None:
        await self.move_to(OPEN_OFFSET)

    @step("4 個目: 回転可能位置へ後退")
    async def work_4_clear_after_place(self) -> None:
        await self.move_to(TO_CLEAR)

    @step("4 個目: 受け取り姿勢へ")
    async def work_4_receive_pose(self) -> None:
        await self.move_to(RECEIVE_POSE)

    @step("初期位置へ復帰")
    async def return_to_retracted(self) -> None:
        await self.move_to(TO_RETRACTED)
