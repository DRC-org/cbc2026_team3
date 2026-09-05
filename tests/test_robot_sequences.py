from __future__ import annotations

import collections
import pathlib
from unittest.mock import AsyncMock, MagicMock

import can
import pytest
import yaml

from lib.drivers.base import ControlMode
from lib.match_state import ROLE_PRE_MATCH
from lib.sequence.engine import Sequence, StepInfo
from lib.sequence.motors import MotorGroup, MotorHandle
from lib.sequence.positions import PositionTable, load_position_table
from sequences.main_hand import MainHandSequence
from sequences.sub_hand import SubHandSequence
from tests.fake_drivers import StubFeedbackDriver

_CONFIG_DIR = pathlib.Path(__file__).resolve().parent.parent / "config"

# 位置定数 yaml とロボット config の対応。両方を突き合わせる試験で共有する
_ROBOTS = [
    ("main_hand_positions.yaml", "main_hand.yaml", MainHandSequence),
    ("sub_hand_positions.yaml", "sub_hand.yaml", SubHandSequence),
]


class _RecordingDriver(StubFeedbackDriver):
    """指令を共有リストに記録し、即座に到達したことにするテスト用ドライバ。"""

    def __init__(self, name: str, sink: list[tuple[str, float]]) -> None:
        super().__init__(name, 1)
        self._sink = sink

    def encode_target(self, mode: ControlMode, value: float) -> can.Message:
        # コンベアのような duty 指令の軸もあるため、モードは限定せずそのまま記録する
        self._sink.append((self.name, value))
        if mode is ControlMode.POSITION:
            self.set_observed(position=value)
        return super().encode_target(mode, value)


def _recording_group(names: list[str]) -> tuple[MotorGroup, list[tuple[str, float]]]:
    mgr = MagicMock()
    mgr.send = AsyncMock()
    sink: list[tuple[str, float]] = []
    group = MotorGroup()
    for name in names:
        group.add(MotorHandle(name, _RecordingDriver(name, sink), mgr, poll_interval=0.001))
    return group, sink


def _motor_names(table: PositionTable) -> list[str]:
    """位置定数表が参照する全モータ名 (論理軸ではモータ名 != 軸名になる)。"""
    names: list[str] = []
    for axis in table.axes:
        for name in table.axis(axis).motor_names:
            if name not in names:
                names.append(name)
    return names


def _wire(seq: Sequence, position_config: dict) -> tuple[list[tuple[str, float]], MotorGroup]:
    table = load_position_table(position_config)
    group, sink = _recording_group(_motor_names(table))
    seq.bind_motors(group)
    seq.bind_positions(table)
    return sink, group


def _axis(**overrides: object) -> dict:
    # 換算を通したことを検証する目的ではないので、この試験では scale=1 の素通しにする
    axis: dict = {"unit": "test", "command_unit": "test", "timeout_s": 0.2}
    axis.update(overrides)
    return axis


def _axes(names: list[str]) -> dict:
    return {name: _axis() for name in names}


def _paired_axis(*motors: tuple[str, float], **overrides: object) -> dict:
    """左右 2 台で 1 動作をする軸。逆回転は scale の符号で表す (本番 yaml と同じ書き方)。"""
    return _axis(
        motors={name: {"scale": scale} for name, scale in motors},
        # 実機の値ではなく「偏差判定が働いても素通しになる」幅。指令値の検証が目的のため
        sync_tolerance=overrides.pop("sync_tolerance", 100.0),
        **overrides,
    )


_MAIN_POSITIONS = {
    "axes": {
        "y_axis": _paired_axis(("y_axis_r", 1.0), ("y_axis_l", -1.0)),
        "rotate": _paired_axis(("rotate_r", 1.0), ("rotate_l", -1.0)),
        "gripper": _axis(),
        # duty 軸は到達判定を持たない。試験では固定待ちを入れない
        "conveyor": _axis(command_mode="duty", settle_s=0.0),
        "wall_f": _axis(),
        "wall_r": _axis(),
    },
    "positions": {
        "y_axis": {
            "home": 11.0,
            "work_1": 12.0,
            "work_2": 12.5,
            "work_3": 13.0,
            "work_shared": 16.0,
            "approach": 14.0,
            "place": 15.0,
        },
        "rotate": {"home": 20.0, "pick": 21.0, "place": 22.0},
        "gripper": {"open": 31.0, "closed": 32.0},
        "conveyor": {"stop": 0.0, "run": 0.4},
        "wall_f": {"initial": 41.0, "closed": 42.0, "open": 43.0},
        "wall_r": {"initial": 44.0, "closed": 45.0, "open": 46.0},
    },
}

# 初期位置は 2 ステップ (先頭と末尾) で共有されるため、期待値も 1 か所にまとめる
_MAIN_HOME_TARGETS = [
    ("y_axis_r", 11.0),
    ("y_axis_l", -11.0),
    ("rotate_r", 20.0),
    ("rotate_l", -20.0),
    ("gripper", 31.0),
    ("wall_f", 41.0),
    ("wall_r", 44.0),
    ("conveyor", 0.0),
]

# 吸着パッドの電磁弁。**6 軸とも同じ定義**で、on_off 軸は到達判定を持たない
# (基板が弁の開閉を観測できない)。試験では固定待ちを入れない
_VALVE_AXES = [f"valve_{i}" for i in range(1, 7)]

_SUB_POSITIONS = {
    "axes": {
        **_axes(["sub_arm_joint", "sub_gripper"]),
        **{name: _axis(command_mode="on_off", settle_s=0.0) for name in _VALVE_AXES},
        "pump_vac": _axis(command_mode="duty", settle_s=0.0),
        "pump_blow": _axis(command_mode="duty", settle_s=0.0),
    },
    "positions": {
        "sub_arm_joint": {"home": 0.0, "extended": 21.0, "handoff": 23.0, "place": 24.0},
        "sub_gripper": {"open": 31.0, "closed": 32.0},
        **{name: {"open": 1.0, "closed": 0.0} for name in _VALVE_AXES},
        "pump_vac": {"stop": 0.0, "run": 0.61},
        "pump_blow": {"stop": 0.0, "run": 0.62},
    },
}


# 全パッドの弁を同じ状態にしたときの指令。sequences/sub_hand.py の _all_valves と
# 同じ順序 (valve_1 → valve_6) で並ぶ
def _valves(value: float) -> list[tuple[str, float]]:
    return [(name, value) for name in _VALVE_AXES]


async def _run_each_step(
    seq: Sequence, position_config: dict
) -> tuple[list[tuple[StepInfo, list[tuple[str, float]]]], MotorGroup]:
    """全ステップを定義順に走らせ、ステップ 1 つぶんの指令に切り分けて返す。

    **メソッド名やラベルを試験側へ書き写さないための入口。** メインハンドの
    シーケンス構成は戦略が未確定のあいだ差し替わり続けるので、名前を 1 対 1 で
    固定すると、機構ではなく試験を直す作業だけが毎回発生する。ここで拾うのは
    「どのステップが何を指令したか」だけで、性質の検証は呼び出し側が行う。
    """
    sink, group = _wire(seq, position_config)
    per_step: list[tuple[StepInfo, list[tuple[str, float]]]] = []
    for info in seq.steps:
        first = len(sink)
        await getattr(seq, info.method_name)()
        per_step.append((info, sink[first:]))
    return per_step, group


def _single_motor_value(table: PositionTable, axis: str, position: str) -> float:
    (value,) = table.commands(axis, position).values()
    return value


def _paired_motor_names(table: PositionTable) -> list[tuple[str, str]]:
    """左右 2 台で 1 動作をする軸のモータ名。**位置定数表から導き、試験へ書き写さない。**"""
    pairs: list[tuple[str, str]] = []
    for axis in table.axes:
        names = table.axis(axis).motor_names
        if len(names) == 2:
            pairs.append((names[0], names[1]))
    return pairs


class TestMainHandSteps:
    """メインハンドのシーケンスが持つべき性質。

    ステップ名・ラベル・列を回る順序は `sequences/main_hand.py` の docstring が
    宣言するとおり暫定なので、ここでは固定しない。固定するのは、差し替えても
    壊れてはいけない側 —— 初期姿勢に始まり初期姿勢に終わること、ワークを持った
    まま開くグリッパが単独指令かつトリガー待ちであること、左右直結ペアが逆符号で
    動くこと、コンベアが duty で回ること。
    """

    async def test_starts_and_ends_at_home(self) -> None:
        """先頭と末尾は同じ初期姿勢であること。

        末尾が初期姿勢でないと、次のサイクルの先頭 (`move_to_home`) が
        「前のサイクルの終了姿勢からいきなり全軸を動かす」ことになる。
        `HOME` を 1 か所に置いてある意味もそこにある。
        """
        per_step, _ = await _run_each_step(MainHandSequence(), _MAIN_POSITIONS)

        assert dict(per_step[0][1]) == dict(_MAIN_HOME_TARGETS)
        assert dict(per_step[-1][1]) == dict(_MAIN_HOME_TARGETS)

    async def test_release_is_a_step_of_its_own(self) -> None:
        """ワークを持ったままグリッパを開くステップは、他の軸を一緒に動かさないこと。

        `move_to` は軸ごとの指令を asyncio.gather で**同時に**出すので、開く指令と
        y 軸の移動指令を 1 回にまとめると、グリッパが開き切る前に機体が走り出して
        ワークを搬送経路の外へ落とす。

        「持ったまま開く」の判定はメソッド名ではなくグリッパの状態遷移
        (閉 -> 開) で行う。名前で拾うと、改名しただけで検査が素通りになる。
        """
        table = load_position_table(_MAIN_POSITIONS)
        gripper = table.axis("gripper").motor_names[0]
        opened = _single_motor_value(table, "gripper", "open")
        closed = _single_motor_value(table, "gripper", "closed")
        per_step, _ = await _run_each_step(MainHandSequence(), _MAIN_POSITIONS)

        holding = False
        releases = 0
        for info, commands in per_step:
            issued = dict(commands)
            if issued.get(gripper) == closed:
                holding = True
                continue
            if issued.get(gripper) != opened or not holding:
                continue
            releases += 1
            holding = False
            assert set(issued) == {gripper}, f"{info.method_name} がリリースと同時に他軸を動かす"

        assert releases > 0, "ワークを持ったまま開くステップが 1 つも無い"

    async def test_grab_and_release_require_trigger(self) -> None:
        """把持とリリースは操縦者の目視確認を挟むこと。

        把持は失敗すると機構破損に直結し、リリースはやり直しが利かない
        (落としたワークは拾えない)。掴めていない / 位置がずれているまま次の
        ステップへ流れると、そのまま搬送・投下まで走る。
        """
        table = load_position_table(_MAIN_POSITIONS)
        gripper = table.axis("gripper").motor_names[0]
        opened = _single_motor_value(table, "gripper", "open")
        closed = _single_motor_value(table, "gripper", "closed")
        per_step, _ = await _run_each_step(MainHandSequence(), _MAIN_POSITIONS)

        holding = False
        checked = 0
        for info, commands in per_step:
            issued = dict(commands)
            if issued.get(gripper) == closed:
                holding = True
            elif issued.get(gripper) == opened and holding:
                holding = False
            else:
                continue
            checked += 1
            assert info.require_trigger is True, f"{info.method_name} がトリガー待ちを持たない"

        # 把持 4 回 + リリース 4 回。初期姿勢の「開いたまま開く」は数に入らない
        assert checked >= 2

    async def test_paired_axes_are_commanded_with_opposite_signs(self) -> None:
        """左右直結のペア軸は、どのステップでも逆符号で指令されること。

        符号を 1 か所落とすと左右が押し合い、その場で機構が壊れる。
        対象の軸は位置定数表 (motors: が 2 台) から導くので、ペア軸が増えても
        試験を書き換えなくてよい。
        """
        table = load_position_table(_MAIN_POSITIONS)
        pairs = _paired_motor_names(table)
        assert pairs, "ペア軸が 1 つも無い位置定数では検査にならない"
        per_step, _ = await _run_each_step(MainHandSequence(), _MAIN_POSITIONS)

        checked = 0
        for info, commands in per_step:
            issued = dict(commands)
            for right, left in pairs:
                if right not in issued or left not in issued:
                    continue
                checked += 1
                assert issued[right] != 0.0, (
                    f"{info.method_name}: {right} が 0 で符号を検査できない"
                )
                assert issued[left] == pytest.approx(-issued[right]), (
                    f"{info.method_name}: {right} と {left} が同符号"
                )

        assert checked > 0

    async def test_conveyor_is_commanded_as_duty(self) -> None:
        """コンベアは DC モータで位置の概念がないため duty で指令されること。

        位置指令で送ると自作 DC 基板は単位を取り違えたまま黙って受け付ける
        (基板はフィードバックを持たないので症状が出ない)。
        """
        seq = MainHandSequence()
        per_step, group = await _run_each_step(seq, _MAIN_POSITIONS)

        run = _single_motor_value(load_position_table(_MAIN_POSITIONS), "conveyor", "run")
        commanded = [
            value for _, commands in per_step for name, value in commands if name == "conveyor"
        ]

        assert group["conveyor"].mode is ControlMode.DUTY
        assert run in commanded, "シーケンス中にコンベアを一度も回していない"


class TestSubHandSteps:
    @pytest.mark.parametrize(
        ("method_name", "expected"),
        [
            # **弁を閉じてから吸気ポンプを回す。** 逆順だと、前サイクルで開いたままの
            # 弁からいきなり吸引が始まり、置いたばかりのワークを吸い直す
            (
                "move_to_home",
                [
                    *_valves(0.0),
                    ("pump_blow", 0.0),
                    ("sub_arm_joint", 0.0),
                    ("sub_gripper", 31.0),
                    ("pump_vac", 0.61),
                ],
            ),
            ("extend_sub_arm", [("sub_arm_joint", 21.0)]),
            ("move_to_handoff", [("sub_arm_joint", 23.0)]),
            ("grip_handoff", [("sub_gripper", 32.0)]),
            ("grip_by_suction", _valves(1.0)),
            ("move_to_place", [("sub_arm_joint", 24.0)]),
            # **吸気ポンプは止めず弁だけを閉じ、残圧は排気で押し離す。**
            # ポンプを止めて解放しようとすると、配管の負圧が抜けるまで張り付く
            (
                "release_at_place",
                [
                    *_valves(0.0),
                    ("pump_blow", 0.62),
                    ("pump_blow", 0.0),
                    ("sub_gripper", 31.0),
                ],
            ),
            (
                "return_home",
                [
                    *_valves(0.0),
                    ("pump_blow", 0.0),
                    ("sub_arm_joint", 0.0),
                    ("sub_gripper", 31.0),
                ],
            ),
        ],
    )
    async def test_step_sends_expected_targets(
        self, method_name: str, expected: list[tuple[str, float]]
    ) -> None:
        seq = SubHandSequence()
        sink, _ = _wire(seq, _SUB_POSITIONS)

        await getattr(seq, method_name)()

        assert sink == expected

    def test_step_labels_unchanged(self) -> None:
        seq = SubHandSequence()

        assert [s["label"] for s in seq.steps_info] == [
            "初期位置へ移動",
            "補助ハンド展開",
            "ワーク受け取り位置へ",
            "ハンド閉じる (受け取り)",
            "ワーク吸着",
            "配置位置へ移動",
            "ワーク解放 (配置)",
            "初期位置へ復帰",
        ]

    def test_grip_requires_trigger(self) -> None:
        """メインハンドと向かい合う唯一の動作なので目視確認を要求する。"""
        seq = SubHandSequence()
        grip = next(s for s in seq.steps_info if s["label"].startswith("ハンド閉じる"))

        assert grip["require_trigger"] is True

    def test_suction_requires_trigger(self) -> None:
        """吸着できたかは PC から観測できないので操縦者の目視確認を要求する。

        電磁弁基板は圧力センサもリミットスイッチも持たず、FEEDBACK の到達フラグも
        立てない (仕様書 §9.3)。弁を開けて settle_s 待つだけなので、吸い付いていなくても
        シーケンスは先へ進む。ここを素通りにすると、ワークを掴んでいないまま
        搬送・配置まで走る。
        """
        seq = SubHandSequence()
        suction = next(s for s in seq.steps_info if s["label"] == "ワーク吸着")

        assert suction["require_trigger"] is True

    async def test_release_does_not_stop_vacuum_pump(self) -> None:
        """解放時に吸気ポンプを止めないこと (仕様書 §9.6: 試合中は回しっぱなし)。

        止めると配管の負圧が抜けるまでワークが張り付き、しかも次のサイクルで
        ポンプの立ち上がりを待つことになる。解放は弁と排気ポンプだけで行う。
        """
        seq = SubHandSequence()
        sink, _ = _wire(seq, _SUB_POSITIONS)

        await seq.release_at_place()

        assert "pump_vac" not in [name for name, _ in sink]


def _load_shipped(yaml_name: str) -> PositionTable:
    return load_position_table(
        yaml.safe_load((_CONFIG_DIR / yaml_name).read_text()), source=yaml_name
    )


def _load_config(robot_config: str) -> dict:
    return yaml.safe_load((_CONFIG_DIR / robot_config).read_text())


class TestShippedPositionYaml:
    """同梱の位置定数 yaml が、シーケンスの参照する軸・位置をすべて持つことを確認する。"""

    @pytest.mark.parametrize(("yaml_name", "robot_config", "sequence_cls"), _ROBOTS)
    async def test_all_steps_run_against_shipped_yaml(
        self, yaml_name: str, robot_config: str, sequence_cls: type[Sequence]
    ) -> None:
        table = _load_shipped(yaml_name)
        seq = sequence_cls()
        group, _ = _recording_group(_motor_names(table))
        seq.bind_motors(group)
        seq.bind_positions(table)

        for info in seq.steps:
            await getattr(seq, info.method_name)()

    @pytest.mark.parametrize(("yaml_name", "robot_config", "sequence_cls"), _ROBOTS)
    def test_axis_motors_exist_in_robot_config(
        self, yaml_name: str, robot_config: str, sequence_cls: type[Sequence]
    ) -> None:
        """論理軸を構成するモータ名は、実機 config に定義済みのモータであること。

        軸名はモータ名でなくてよい (y_axis は y_axis_r / y_axis_l の 2 台で駆動する) ため、
        突き合わせるのは軸名ではなく AxisSpec.motor_names。
        """
        table = _load_shipped(yaml_name)
        motors = _load_config(robot_config)["motors"]

        assert set(_motor_names(table)) <= set(motors)

    async def test_main_hand_paired_axes_command_opposite_signs(self) -> None:
        """同梱 yaml でも左右ペア軸が逆符号で指令されること (符号を落とすと機構が壊れる)。"""
        table = _load_shipped("main_hand_positions.yaml")

        for axis, position in (("y_axis", "work_shared"), ("rotate", "pick")):
            commands = list(table.commands(axis, position).values())
            assert len(commands) == 2
            assert commands[0] != 0.0
            assert commands[1] == pytest.approx(-commands[0])


class TestShippedRobotConfig:
    def test_can_ids_are_unique_per_bus_across_robots(self) -> None:
        """can_id はバス単位でロボット横断に一意であること。

        can_edulite / can_generic はメインハンドとサブハンドで物理的に同じバスを共有する。
        重複すると CANManager._receive_loop が最初にマッチした 1 台で break し、
        もう一方は永久にフィードバックを得られない (EDULITE では無励磁のまま運用に入る)。
        """
        owners: dict[tuple[str, int], list[str]] = collections.defaultdict(list)

        for path in sorted(_CONFIG_DIR.glob("*.yaml")):
            config = yaml.safe_load(path.read_text()) or {}
            motors = config.get("motors")
            if not motors:
                continue
            buses = config.get("can_buses") or {}
            for motor_name, motor in motors.items():
                # バス別名 (m3508_bus 等) ではなく実インタフェース名で束ねる。
                # 別名が違っていても同じ物理バスなら衝突するため
                interface = buses.get(motor["bus"], motor["bus"])
                owners[(interface, int(motor["can_id"]))].append(f"{path.name}:{motor_name}")

        duplicated = {key: names for key, names in owners.items() if len(names) > 1}

        assert duplicated == {}

    def test_motor_names_are_unique_across_robots(self) -> None:
        """モータ名はロボット横断に一意であること。

        サーバーはモータを名前で引く。``_find_position_loop`` は全ロボットの位置制御
        ループを名前だけで走査して最初に見つかったものを返すため、同名が両機に居ると
        片方は永久に PID を差し替えられない (症状は「送信しても効かない」だけで、
        拒否も出ないので画面からは原因が分からない)。

        ヘルス表示・動作確認・チェックリストも同じく名前で突き合わせているので、
        can_id と同様ここで固定しておく。
        """
        owners: dict[str, list[str]] = collections.defaultdict(list)

        for path in sorted(_CONFIG_DIR.glob("*.yaml")):
            config = yaml.safe_load(path.read_text()) or {}
            for section in ("motors", "sensors"):
                for name in config.get(section) or {}:
                    owners[name].append(f"{path.name}:{section}")

        duplicated = {name: places for name, places in owners.items() if len(places) > 1}

        assert duplicated == {}

    def test_axis_names_are_unique_across_robots(self) -> None:
        """論理軸名もロボット横断に一意であること。

        統合動作確認シーケンス (sequences/motor_check.py) は両ハンドのアクチュエータを
        1 つの順序で駆動するため、両機の位置定数を 1 つの表へ束ねる
        (``PositionTable.merged``)。衝突していると起動が拒否される。

        ここで先に落としておくのは、起動時の失敗だと「試合直前に config を直す」
        状況になりうるため。
        """
        owners: dict[str, list[str]] = collections.defaultdict(list)

        for path in sorted(_CONFIG_DIR.glob("*_positions.yaml")):
            config = yaml.safe_load(path.read_text()) or {}
            for name in config.get("axes") or {}:
                owners[name].append(path.name)

        duplicated = {name: places for name, places in owners.items() if len(places) > 1}

        assert duplicated == {}

    def test_homing_sensors_are_registered(self) -> None:
        """`homing.sensor` に書いた名前が config の `sensors:` に居ること。

        居ないと零点確定は「センサが応答していません」で必ず失敗する。しかも
        症状は配線不良と区別が付かないので、実機の前で切り分けることになる。

        rotate の homing はハード追加待ちでコメントアウトしてある。外すときに
        `sensors:` への追加を忘れると、ここで落ちる。
        """
        sensors: set[str] = set()
        for path in sorted(_CONFIG_DIR.glob("*.yaml")):
            if path.name.endswith("_positions.yaml"):
                continue
            config = yaml.safe_load(path.read_text()) or {}
            sensors |= set(config.get("sensors") or {})

        required: set[str] = set()
        for path in sorted(_CONFIG_DIR.glob("*_positions.yaml")):
            table = _load_shipped(path.name)
            for axis in table.axes:
                homing = table.axis(axis).homing
                if homing is not None:
                    required.add(homing.sensor)

        assert required <= sensors

    def test_paired_axis_motors_agree_on_set_zero_on_start(self) -> None:
        """左右ペア軸を構成するモータは `set_zero_on_start` が揃っていること。

        起動時の `set_zero` は原点をその場へ付け替える。逆回転ペアは `scale` の符号で
        向きを表すので、**片側だけ付け替えると揃うどころか機械ゼロの差がまるごと
        偏差として残る** —— 物理的にずれ 0 のまま `SyncMonitor` が全体緊急停止を掛け、
        機体は 1 ステップも動かせない (実機で 175.879deg を踏んでいる)。

        「ペア軸に片側だけ効く操作を作らない」を config の側でも守るための試験。
        本番と机上ベンチ (config/bench/<対象>/) の全セットを見るのは、ベンチ config を
        誰も検証していない時期があり、気付くのが机上に基板を並べた当日だったため。

        値は `lib/config_schema.py` の既定に合わせて「書かなければ False」で読む。
        書き忘れた側が既定へ落ちる形の食い違いも、ここで同じように落ちる。
        """
        mismatched: dict[str, dict[str, bool]] = {}
        inspected: set[str] = set()

        for positions_path in sorted(_CONFIG_DIR.rglob("*_positions.yaml")):
            robot_path = positions_path.with_name(
                positions_path.name.removesuffix("_positions.yaml") + ".yaml"
            )
            motors = (yaml.safe_load(robot_path.read_text()) or {}).get("motors") or {}
            table = load_position_table(
                yaml.safe_load(positions_path.read_text()), source=str(positions_path)
            )

            for axis in table.axes:
                names = [name for name in table.axis(axis).motor_names if name in motors]
                if len(names) < 2:
                    continue
                key = f"{robot_path.relative_to(_CONFIG_DIR)}:{axis}"
                inspected.add(key)
                flags = {name: bool(motors[name].get("set_zero_on_start", False)) for name in names}
                if len(set(flags.values())) > 1:
                    mismatched[key] = flags

        assert mismatched == {}

        # 走査そのものが壊れたら気付けるようにする。ファイル名の規約 (位置定数は
        # <robot_name>_positions.yaml、robot config は同じディレクトリの <robot_name>.yaml)
        # が崩れると、上のループは 1 軸も見ないまま緑を返す。
        # **ベンチセットを足したらここへも足すこと** (手書きの一覧なので追従が要る。
        # tests/test_config_schema.py の _BENCH_DIRS が同じ性質を持つ)。
        # bench/main_hand は実測値を本番へ移したことで自前の positions を持たなく
        # なった (rglob は `*_positions.yaml` を起点にするので、もう対象に現れない)。
        # 本番の main_hand.yaml:y_axis / :rotate は含んでいるので検査そのものは続く。
        assert {
            "main_hand.yaml:y_axis",
            "main_hand.yaml:rotate",
            "bench/edulite/main_hand.yaml:rotate",
            "bench/m3508/main_hand.yaml:y_axis",
        } <= inspected

    def test_checklist_covers_what_cannot_be_judged_automatically(self) -> None:
        """自動判定できないものは、すべて目視確認項目で埋めること。

        統合動作確認シーケンス (sequences/motor_check.py) は全アクチュエータを動かすが、
        到達判定を持つのは位置指令の軸だけ。duty (DC 基板) と on_off (電磁弁) は
        `settle_s` の固定待ちへ落ちるので、**動いたことを機械は誰も見ていない**。
        センサも同じで、死んだまま原点合わせを始めると「いつまでも当たらない」
        形でしか分からない。
        """
        checklist = yaml.safe_load((_CONFIG_DIR / "checklist.yaml").read_text())
        ids = {item["id"] for item in checklist["checklists"][ROLE_PRE_MATCH]}

        assert {
            # メインハンド: ペア軸 (y_axis / rotate)・DC 基板・原点センサ
            "y_axis_sync",
            "rotate_sync",
            "wall_initial",
            "conveyor_stop",
            "conveyor_run",
            "origin_sensor_react",
            # サブハンド: 電磁弁 (到達を観測できない) と DC 基板のポンプ
            "valves_closed",
            "valves_actuate",
            "pumps_run",
            "suction_hold",
            "suction_release",
        } <= ids
