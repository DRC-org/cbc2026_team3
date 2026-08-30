"""セッティングタイムのアクチュエータ動作確認(両ハンド統合)。

**両ハンドを 1 本のシーケンスで順に駆動する。** かつては機体ごとに独立した
動作確認 (`lib/motor_check.py` の `MotorCheckRunner`) を持っていたが、2 つを
同時に起動できるため両ハンドが同時に動き、可動域の重なる位置で干渉しうる。
順序を 1 本に固定すれば、いつ何が動くかがシーケンスの並びから読める。

**判定はシーケンスエンジンがそのまま担う。** `move_to` は位置定数の `tolerance`
で到達を判定し、到達しなければ `SequenceTimeoutError`、到達しても左右がずれていれば
`AxisSyncError` を投げる。到達判定を持たない軸 (`duty` / `on_off`) は `settle_s` の
固定待ちへ落ちる。つまり「フィードバックを持つものは自動判定、持たないものは目視」
という切り分けが、追加の実装なしで成立している。目視ぶんは
`config/checklist.yaml` の指差喚呼が受け持つ。

**数値をこのファイルに書かない。** 確認するのは「指令どおり動くか」なので、
運用で使う位置名へ動かせば足りる。新しい試験用の値を作ると、その値だけが
機構の変更に取り残される (動作確認では動くのに運用では動かない、が成立する)。

**必ず初期姿勢で終わる。** 途中の姿勢のまま終わると、操縦者は試合開始前に
手動で戻すことになる。各ハンドの最後を初期姿勢への復帰にしてある。
"""

from __future__ import annotations

import logging

from lib.sequence.engine import Sequence, step
from lib.sequence.homing import HomingRunner
from lib.sequence.motors import AxisHandle

logger = logging.getLogger(__name__)

#: 電磁弁の軸名。6 個を 1 ステップで順に開閉する。
#: 打音と動作の目視は `config/checklist.yaml` の `valves_actuate` が受け持つ
#: (基板は弁が開いたかを観測できないので、到達フラグは立たない)。
VALVE_AXES: tuple[str, ...] = (
    "valve_1",
    "valve_2",
    "valve_3",
    "valve_4",
    "valve_5",
    "valve_6",
)

#: メインハンドの初期姿勢。`robots/main_hand.py` の `move_to_home` と同じ組み合わせ。
MAIN_HOME: dict[str, str] = {
    "y_axis": "home",
    "rotate": "home",
    "gripper": "open",
    "wall_f": "initial",
    "wall_r": "initial",
    "conveyor": "stop",
}

#: サブハンドの初期姿勢。電磁弁とポンプは「止める = 消磁 / 停止」に倒す。
SUB_HOME: dict[str, str] = {
    "sub_arm_joint": "home",
    "sub_y_axis": "home",
    "sub_lift": "home",
    "sub_gripper": "open",
    "pump_vac": "stop",
    "pump_blow": "stop",
}


class MotorCheckSequence(Sequence):
    """全アクチュエータを順に駆動して確認するシーケンス。

    通常のシーケンスと違い `require_trigger` を使わない。20 個以上のアクチュエータを
    1 つずつ操縦者に送らせると、確認そのものより操作の手数が多くなる。代わりに
    起動前の確認ダイアログで 1 度だけ意思確認し、以後は最後まで流す。
    止めたいときは中断 (`motor_check_abort`) で降ろす。
    """

    def __init__(self, name: str = "motor_check") -> None:
        super().__init__(name)
        self._homing: HomingRunner | None = None

    def bind_homing(self, runner: HomingRunner) -> None:
        """零点確定の実行口を注入する。

        未注入でもシーケンスは走る (ホーミングのステップが「対象なし」で素通りする)。
        机上ベンチや、センサをまだ配線していない構成で動作確認だけ試せるようにするため。
        """
        self._homing = runner

    # ------------------------------------------------------------------ #
    #  零点確定
    # ------------------------------------------------------------------ #

    @step("リミットスイッチで零点を確定する")
    async def home_axes(self) -> None:
        """`homing:` を書いた軸をリミットスイッチまで寄せ、原点を切り直す。

        **必ず最初に走らせる。** 零点が未確定のまま位置指令を出すと、電源投入位置を
        原点とみなして全ステップが同じだけずれた場所へ動く。

        失敗 (`HomingError`) はシーケンスを止める。原点がずれたまま走るより、
        動かないまま止まって操縦者に知らせるほうが安全なので、握り潰さない。
        """
        if self._homing is None:
            logger.info("[motor_check] 零点確定: 実行口が未注入のため飛ばす")
            return

        table = self.positions
        targets = [name for name in table.axes if table.axis(name).homing is not None]
        if not targets:
            logger.info("[motor_check] 零点確定: homing を持つ軸が無いため飛ばす")
            return

        for axis in targets:
            spec = table.axis(axis)
            logger.info("[motor_check] 零点確定: %s", axis)
            handle = AxisHandle(spec, [getattr(self.motors, name) for name in spec.motor_names])
            await self._homing.home(spec, handle)

    # ------------------------------------------------------------------ #
    #  メインハンド
    # ------------------------------------------------------------------ #

    @step("メインハンド 初期姿勢へ")
    async def main_home(self) -> None:
        logger.info("[motor_check] メインハンド 初期姿勢へ")
        await self.move_to(MAIN_HOME)

    @step("メインハンド y 軸 (左右直結ペア)")
    async def main_y_axis(self) -> None:
        # 左右 2 台が機構的に直結している。**軸単位で 1 回だけ指令する**
        # (モータ単位で 1 台ずつ動かすと、その場で機構が壊れる)
        logger.info("[motor_check] メインハンド y 軸")
        await self.move_to({"y_axis": "work_3"})
        await self.move_to({"y_axis": "home"})

    @step("メインハンド エンドエフェクタ回転 (左右直結ペア)")
    async def main_rotate(self) -> None:
        logger.info("[motor_check] メインハンド エンドエフェクタ回転")
        await self.move_to({"rotate": "pick"})
        await self.move_to({"rotate": "home"})

    @step("メインハンド グリッパ")
    async def main_gripper(self) -> None:
        logger.info("[motor_check] メインハンド グリッパ")
        await self.move_to({"gripper": "closed"})
        await self.move_to({"gripper": "open"})

    @step("メインハンド 壁 前後")
    async def main_walls(self) -> None:
        logger.info("[motor_check] メインハンド 壁 前後")
        await self.move_to({"wall_f": "closed", "wall_r": "closed"})
        await self.move_to({"wall_f": "open", "wall_r": "open"})
        await self.move_to({"wall_f": "initial", "wall_r": "initial"})

    @step("メインハンド コンベア (目視確認)")
    async def main_conveyor(self) -> None:
        # DC 基板はフィードバックを一切持たない。回ったかどうかは
        # `config/checklist.yaml` の conveyor_run / conveyor_stop で目視確認する
        logger.info("[motor_check] メインハンド コンベア")
        await self.move_to({"conveyor": "run"})
        await self.move_to({"conveyor": "stop"})

    # ------------------------------------------------------------------ #
    #  サブハンド
    # ------------------------------------------------------------------ #

    @step("サブハンド 初期姿勢へ")
    async def sub_home(self) -> None:
        logger.info("[motor_check] サブハンド 初期姿勢へ")
        await self.move_to(SUB_HOME)

    @step("サブハンド アーム関節")
    async def sub_arm(self) -> None:
        logger.info("[motor_check] サブハンド アーム関節")
        await self.move_to({"sub_arm_joint": "extended"})
        await self.move_to({"sub_arm_joint": "home"})

    @step("サブハンド 前後スライド (Y 方向)")
    async def sub_y_axis(self) -> None:
        # Damiao DM3520。位置ループはドライバ内蔵なので、到達判定は
        # 位置定数の tolerance (1mm) にそのまま乗る。
        # **ここが落ちるときは config の p_max を最初に疑う** —— レジスタ 0x15 と
        # ずれていると位置が比例倍で読め、指令どおり動いても到達しない
        logger.info("[motor_check] サブハンド 前後スライド")
        await self.move_to({"sub_y_axis": "extended"})
        await self.move_to({"sub_y_axis": "home"})

    @step("サブハンド 昇降")
    async def sub_lift(self) -> None:
        # 前後軸と別ステップにするのは、2 軸を同時に動かすと機構の姿勢が
        # 1 ステップで 2 つ変わり、どちらが引っかかったのか目で追えなくなるため。
        # **上げてから必ず下ろす** —— 上がったまま次のステップへ進むと、
        # 以降の確認をすべて持ち上がった姿勢で行うことになる
        logger.info("[motor_check] サブハンド 昇降")
        await self.move_to({"sub_lift": "lifted"})
        await self.move_to({"sub_lift": "home"})

    @step("サブハンド 補助ハンド")
    async def sub_gripper(self) -> None:
        logger.info("[motor_check] サブハンド 補助ハンド")
        await self.move_to({"sub_gripper": "closed"})
        await self.move_to({"sub_gripper": "open"})

    @step("サブハンド 電磁弁 6 個 (打音・目視確認)")
    async def sub_valves(self) -> None:
        # 1 個ずつ順に開閉する。まとめて開くと、どれが鳴っていないのか分からない
        logger.info("[motor_check] サブハンド 電磁弁 6 個")
        for axis in VALVE_AXES:
            await self.move_to({axis: "open"})
            await self.move_to({axis: "closed"})

    @step("サブハンド 吸気・排気ポンプ (聴音確認)")
    async def sub_pumps(self) -> None:
        # 2 台を同時に回さない。片方ずつでないと、どちらが鳴っているか聞き分けられない
        logger.info("[motor_check] サブハンド 吸気・排気ポンプ")
        await self.move_to({"pump_vac": "run"})
        await self.move_to({"pump_vac": "stop"})
        await self.move_to({"pump_blow": "run"})
        await self.move_to({"pump_blow": "stop"})

    # ------------------------------------------------------------------ #
    #  後始末
    # ------------------------------------------------------------------ #

    @step("両ハンドを初期姿勢へ戻す")
    async def restore_home(self) -> None:
        # 途中の姿勢で終わると、操縦者が試合開始前に手動で戻すことになる。
        # ここは最後のステップなので、機体が確実に初期姿勢に居ることまでを保証する
        logger.info("[motor_check] 両ハンドを初期姿勢へ戻す")
        await self.move_to({**MAIN_HOME, **SUB_HOME})
