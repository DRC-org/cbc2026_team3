from __future__ import annotations

import logging

from lib.sequence.engine import Sequence, step
from lib.suction import SuctionSelection, SuctionSelectionError

logger = logging.getLogger(__name__)

VALVE_AXES: tuple[str, ...] = ("valve_1", "valve_2", "valve_3", "valve_4", "valve_5", "valve_6")

# 置く箱の順は 2 -> 3 -> 1 -> 4 (2026-09-11)。箱 1 は後回しにする。
# 吸着したら 10mm だけ上げて棚から離し、後退してから移動高さへ上げる (棚の上では
# 高く上げられない)。回すのは上げてから (低いままだとワークが箱に当たる。2026-09-11 実機)。
# 軸どうしの干渉を止める宣言は無いので、守っているのはこの並びだけ。
TO_SHELF: dict[str, str] = {"sub_y_axis": "receive"}
TO_CLEAR: dict[str, str] = {"sub_y_axis": "clear"}
TO_RETRACTED: dict[str, str] = {"sub_y_axis": "retracted"}
TO_HOME: dict[str, str] = {"sub_y_axis": "home"}

DOWN_TO_PICK: dict[str, str] = {"sub_lift": "pick"}
UP_TO_TOP: dict[str, str] = {"sub_lift": "top"}
LIFT_OFF_SHELF: dict[str, str] = {"sub_lift": "lifted"}

CARRY_POSE: dict[str, str] = {"sub_rotate": "carry"}
RECEIVE_POSE: dict[str, str] = {"sub_rotate": "receive"}
# 箱 1 は carry のまま下ろすと当たるので、入れる角度へ傾けてから前へ出す (2026-09-12)。
# 回すのは前端から 150mm 離れた clear で、持ち方の開閉は carry でしか行わない
INSERT_1_POSE: dict[str, str] = {"sub_rotate": "insert_1"}
WALL_R_INITIAL: dict[str, str] = {"wall_r": "initial"}
WALL_R_OPEN: dict[str, str] = {"wall_r": "open"}
# メインハンドの壁 (config/system.yaml の shared_axes で借りている)。吸うあいだだけ
# 押し込んでワークを押さえる (#224)
WALL_F_ASSIST: dict[str, str] = {"wall_f": "assist"}
WALL_F_RELEASE: dict[str, str] = {"wall_f": "closed"}

# ピッチとオフセットは同時に動かさない。閉じるとき オフセット -> ピッチ、
# 開くとき ピッチ -> オフセット。
CLOSE_OFFSET: dict[str, str] = {"sub_offset": "close"}
CLOSE_PITCH: dict[str, str] = {"sub_pitch": "close"}
OPEN_PITCH: dict[str, str] = {"sub_pitch": "open"}
OPEN_OFFSET: dict[str, str] = {"sub_offset": "open"}

PUMP_RUN: dict[str, str] = {"pump_vac": "run"}


def _all_valves(state: str) -> dict[str, str]:
    return dict.fromkeys(VALVE_AXES, state)


# **並び順が意味を持つ。** 昇降を寄せてから前後、後退しきってから回転の順に戻す。
# どの姿勢から押されても干渉制約を踏まないのはこの順序だけである。
# 高さは零点確定を終えた位置と同じ pick、前後はその 15cm 後ろ
# ピッチとオフセットは回転が carry のときしか動かせない (棒を伸ばしたので receive では
# 棚側と当たる。2026-09-12)。守っているのはこの並びだけ
INITIAL_BEFORE_OPEN: tuple[dict[str, str], ...] = (
    _all_valves("closed") | PUMP_RUN,
    DOWN_TO_PICK,
    TO_HOME,
)
# 開くために carry へ回る往復。既に両方開いていれば省く (毎回 5 秒ほど掛かる。2026-09-12)
OPEN_VIA_CARRY: tuple[dict[str, str], ...] = (CARRY_POSE, OPEN_PITCH, OPEN_OFFSET)
INITIAL_AFTER_OPEN: tuple[dict[str, str], ...] = (RECEIVE_POSE, WALL_R_INITIAL)
INITIAL_POSES: tuple[dict[str, str], ...] = (
    *INITIAL_BEFORE_OPEN,
    *OPEN_VIA_CARRY,
    *INITIAL_AFTER_OPEN,
)


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
        open_valves = _all_valves("closed") | dict.fromkeys(enabled, "open")
        # 吸い始めと同時にメインハンドの壁で押し付け、吸い付くまでの待ちは弁の settle_s
        # 1 回ぶんだけ (前は弁→壁→弁で待ちが 2 回入り 6 秒掛かった。2026-09-12)
        await self.move_to(open_valves | WALL_F_ASSIST)

    def _log_placed_position(self) -> None:
        """下ろした前後の位置を残す。あとから位置定数を詰めるのに使う。"""
        try:
            reading = self.motors.axis_state("sub_y_axis")
        except RuntimeError:
            # 機体に繋がっていない (机上のステップ検査など)。ログだけなので黙って抜ける
            return
        if reading.value is None:
            logger.info("[%s] 箱へ下ろした前後の位置: 読めていません", self.name)
            return
        logger.info("[%s] 箱へ下ろした前後の位置: %.2fmm", self.name, reading.value)

    async def _lower_lift(self, name: str) -> None:
        """昇降を name へ下げる。微調整で既にそれより下に居れば動かさない。

        止まっている間に下げて詰めた後は、一度 name まで上がってから次で下りていた
        (2026-09-12)。実測が読めなければ普通に下げる。
        """
        if self._lift_below(name):
            logger.info("[%s] sub_lift は既に %s より下に居るので動かさない", self.name, name)
            return
        await self.move_to({"sub_lift": name})

    def _lift_below(self, name: str) -> bool:
        """昇降の実測が位置名の値より下 (+ 側) に居るか。読めなければ False。"""
        try:
            reading = self.motors.axis_state("sub_lift")
            value = self.positions.raw("sub_lift", name, court=self.court)
        except Exception:
            return False
        return reading.value is not None and reading.value > value

    async def _release(self) -> None:
        # 三方弁は閉じた側がパッドを大気開放するので、閉じるだけで残圧が抜けてワークが離れる
        await self.move_to(_all_valves("closed"))

    @step("初期位置へ移動")
    async def move_to_initial(self) -> None:
        for targets in INITIAL_BEFORE_OPEN:
            await self.move_to(targets)
        # ピッチとオフセットの今の位置を見て、既に開いていれば carry へ回る往復を省く。
        # 読めなければ従来どおり回って開く (閉じたまま棚へ行くと当たる)
        if self._axis_at("sub_pitch", "open") and self._axis_at("sub_offset", "open"):
            logger.info("[%s] ピッチとオフセットは開いているので carry へ回らない", self.name)
        else:
            for targets in OPEN_VIA_CARRY:
                await self.move_to(targets)
        for targets in INITIAL_AFTER_OPEN:
            await self.move_to(targets)

    def _axis_at(self, axis: str, name: str) -> bool:
        """軸の実測が位置名の値に (到達許容差の内で) 居るか。読めなければ False。"""
        try:
            reading = self.motors.axis_state(axis)
            spec = self.positions.axis(axis)
            value = self.positions.raw(axis, name, court=self.court)
        except Exception:
            return False
        if reading.value is None:
            return False
        tolerance = spec.tolerance if spec.tolerance is not None else 1.0
        return abs(reading.value - value) <= tolerance

    @step("1 個目: 棚へ寄せる", require_trigger=True)
    async def work_1_to_shelf(self) -> None:
        # 後壁が閉じたままだと棚へ入れない
        await self.move_to(WALL_R_OPEN)
        await self.move_to(TO_SHELF)

    @step("1 個目: 吸着高さへ下降")
    async def work_1_down_to_pick(self) -> None:
        await self.move_to(DOWN_TO_PICK)

    @step("1 個目: ワーク吸着")
    async def work_1_grip(self) -> None:
        await self._grip_by_suction()

    @step("1 個目: 吸えたか確認して持ち上げ", require_trigger=True)
    async def work_1_lift_up(self) -> None:
        await self.move_to(LIFT_OFF_SHELF)

    @step("1 個目: 回転可能位置へ後退")
    async def work_1_clear_before_turn(self) -> None:
        await self.move_to(TO_CLEAR)
        # 前後が離れてから壁を戻す (押し付けたまま後退すると擦る)
        await self.move_to(WALL_F_RELEASE)

    @step("1 個目: 移動高さへ上昇")
    async def work_1_up_before_turn(self) -> None:
        await self.move_to(UP_TO_TOP)

    @step("1 個目: 搬送姿勢へ")
    async def work_1_carry_pose(self) -> None:
        await self.move_to(CARRY_POSE)

    @step("1 個目: 箱 2 の上へ")
    async def work_1_over_box(self) -> None:
        await self.move_to({"sub_y_axis": "place_2"})

    @step("1 個目: オフセットを閉じる")
    async def work_1_close_offset(self) -> None:
        await self.move_to(CLOSE_OFFSET)

    @step("1 個目: ピッチを閉じる")
    async def work_1_close_pitch(self) -> None:
        await self.move_to(CLOSE_PITCH)

    @step("1 個目: 箱の縁の高さへ下降", require_trigger=True)
    async def work_1_down_to_above_box(self) -> None:
        await self._lower_lift("above_box")

    @step("1 個目: 箱へ下降", require_trigger=True)
    async def work_1_down_to_place(self) -> None:
        await self._lower_lift("place")
        self._log_placed_position()

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
        # 次のワークを吸う高さまで下ろしておく。top のままだと棚へ突っ込む
        await self.move_to(DOWN_TO_PICK)

    @step("2 個目: 棚へ寄せる", require_trigger=True)
    async def work_2_to_shelf(self) -> None:
        # 後壁が閉じたままだと棚へ入れない
        await self.move_to(WALL_R_OPEN)
        await self.move_to(TO_SHELF)

    @step("2 個目: 吸着高さへ下降")
    async def work_2_down_to_pick(self) -> None:
        await self.move_to(DOWN_TO_PICK)

    @step("2 個目: ワーク吸着")
    async def work_2_grip(self) -> None:
        await self._grip_by_suction()

    @step("2 個目: 吸えたか確認して持ち上げ", require_trigger=True)
    async def work_2_lift_up(self) -> None:
        await self.move_to(LIFT_OFF_SHELF)

    @step("2 個目: 回転可能位置へ後退")
    async def work_2_clear_before_turn(self) -> None:
        await self.move_to(TO_CLEAR)
        # 前後が離れてから壁を戻す (押し付けたまま後退すると擦る)
        await self.move_to(WALL_F_RELEASE)

    @step("2 個目: 移動高さへ上昇")
    async def work_2_up_before_turn(self) -> None:
        await self.move_to(UP_TO_TOP)

    @step("2 個目: 搬送姿勢へ")
    async def work_2_carry_pose(self) -> None:
        await self.move_to(CARRY_POSE)

    @step("2 個目: 箱 3 の上へ")
    async def work_2_over_box(self) -> None:
        await self.move_to({"sub_y_axis": "place_3"})

    @step("2 個目: オフセットを閉じる")
    async def work_2_close_offset(self) -> None:
        await self.move_to(CLOSE_OFFSET)

    @step("2 個目: ピッチを閉じる")
    async def work_2_close_pitch(self) -> None:
        await self.move_to(CLOSE_PITCH)

    @step("2 個目: 箱の縁の高さへ下降", require_trigger=True)
    async def work_2_down_to_above_box(self) -> None:
        await self._lower_lift("above_box")

    @step("2 個目: 箱へ下降", require_trigger=True)
    async def work_2_down_to_place(self) -> None:
        await self._lower_lift("place")
        self._log_placed_position()

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
        # 次のワークを吸う高さまで下ろしておく。top のままだと棚へ突っ込む
        await self.move_to(DOWN_TO_PICK)

    @step("3 個目: 棚へ寄せる", require_trigger=True)
    async def work_3_to_shelf(self) -> None:
        # 後壁が閉じたままだと棚へ入れない
        await self.move_to(WALL_R_OPEN)
        await self.move_to(TO_SHELF)

    @step("3 個目: 吸着高さへ下降")
    async def work_3_down_to_pick(self) -> None:
        await self.move_to(DOWN_TO_PICK)

    @step("3 個目: ワーク吸着")
    async def work_3_grip(self) -> None:
        await self._grip_by_suction()

    @step("3 個目: 吸えたか確認して持ち上げ", require_trigger=True)
    async def work_3_lift_up(self) -> None:
        await self.move_to(LIFT_OFF_SHELF)

    @step("3 個目: 回転可能位置へ後退")
    async def work_3_clear_before_turn(self) -> None:
        await self.move_to(TO_CLEAR)
        # 前後が離れてから壁を戻す (押し付けたまま後退すると擦る)
        await self.move_to(WALL_F_RELEASE)

    @step("3 個目: 移動高さへ上昇")
    async def work_3_up_before_turn(self) -> None:
        await self.move_to(UP_TO_TOP)

    @step("3 個目: 搬送姿勢へ")
    async def work_3_carry_pose(self) -> None:
        await self.move_to(CARRY_POSE)

    # 箱 1 は前端に近く、そこで回せない。持ち方を閉じて傾けるのを clear で済ませてから出す
    @step("3 個目: オフセットを閉じる")
    async def work_3_close_offset(self) -> None:
        await self.move_to(CLOSE_OFFSET)

    @step("3 個目: ピッチを閉じる")
    async def work_3_close_pitch(self) -> None:
        await self.move_to(CLOSE_PITCH)

    @step("3 個目: 箱 1 へ入れる角度へ回す")
    async def work_3_insert_pose(self) -> None:
        await self.move_to(INSERT_1_POSE)

    @step("3 個目: 箱 1 の上へ")
    async def work_3_over_box(self) -> None:
        await self.move_to({"sub_y_axis": "place_1"})

    @step("3 個目: 箱の縁の高さへ下降", require_trigger=True)
    async def work_3_down_to_above_box(self) -> None:
        await self._lower_lift("above_box")

    @step("3 個目: 箱へ下降", require_trigger=True)
    async def work_3_down_to_place(self) -> None:
        await self._lower_lift("place")
        self._log_placed_position()

    @step("3 個目: ワーク解放 (配置)", require_trigger=True)
    async def work_3_release(self) -> None:
        await self._release()

    @step("3 個目: 上昇")
    async def work_3_up_to_top(self) -> None:
        await self.move_to(UP_TO_TOP)

    @step("3 個目: 回転可能位置へ後退")
    async def work_3_clear_after_place(self) -> None:
        await self.move_to(TO_CLEAR)

    @step("3 個目: 搬送姿勢へ戻す")
    async def work_3_carry_pose_back(self) -> None:
        await self.move_to(CARRY_POSE)

    @step("3 個目: ピッチを開く")
    async def work_3_open_pitch(self) -> None:
        await self.move_to(OPEN_PITCH)

    @step("3 個目: オフセットを開く")
    async def work_3_open_offset(self) -> None:
        await self.move_to(OPEN_OFFSET)

    @step("3 個目: 受け取り姿勢へ")
    async def work_3_receive_pose(self) -> None:
        await self.move_to(RECEIVE_POSE)
        # 次のワークを吸う高さまで下ろしておく。top のままだと棚へ突っ込む
        await self.move_to(DOWN_TO_PICK)

    @step("4 個目: 棚へ寄せる", require_trigger=True)
    async def work_4_to_shelf(self) -> None:
        # 後壁が閉じたままだと棚へ入れない
        await self.move_to(WALL_R_OPEN)
        await self.move_to(TO_SHELF)

    @step("4 個目: 吸着高さへ下降")
    async def work_4_down_to_pick(self) -> None:
        await self.move_to(DOWN_TO_PICK)

    @step("4 個目: ワーク吸着")
    async def work_4_grip(self) -> None:
        await self._grip_by_suction()

    @step("4 個目: 吸えたか確認して持ち上げ", require_trigger=True)
    async def work_4_lift_up(self) -> None:
        await self.move_to(LIFT_OFF_SHELF)

    @step("4 個目: 回転可能位置へ後退")
    async def work_4_clear_before_turn(self) -> None:
        await self.move_to(TO_CLEAR)
        # 前後が離れてから壁を戻す (押し付けたまま後退すると擦る)
        await self.move_to(WALL_F_RELEASE)

    @step("4 個目: 移動高さへ上昇")
    async def work_4_up_before_turn(self) -> None:
        await self.move_to(UP_TO_TOP)

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

    @step("4 個目: 箱の縁の高さへ下降", require_trigger=True)
    async def work_4_down_to_above_box(self) -> None:
        await self._lower_lift("above_box")

    @step("4 個目: 箱へ下降", require_trigger=True)
    async def work_4_down_to_place(self) -> None:
        await self._lower_lift("place")
        self._log_placed_position()

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
        # 次のワークを吸う高さまで下ろしておく。top のままだと棚へ突っ込む
        await self.move_to(DOWN_TO_PICK)

    @step("初期位置へ復帰")
    async def return_to_retracted(self) -> None:
        await self.move_to(TO_HOME)
