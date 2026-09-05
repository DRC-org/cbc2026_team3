"""統合動作確認シーケンス (sequences/motor_check.py)。

**実際に出荷する config で成立することを見る。** シーケンスが参照する軸名・位置名は
`config/*_positions.yaml` にしか存在しないので、片方だけ直すと「動作確認を押した
瞬間に PositionLookupError で止まる」形でしか現れない。しかもセッティングタイムに
発覚するため、直す時間が無い。

指令の収集に `move_to` の差し替えを使うのは、CAN もモータも無しで全ステップを
1 度ずつ通すため。到達待ちの実装ではなく **どこへ指令するか** を見たい。
"""

from __future__ import annotations

import pathlib
from collections.abc import Collection, Mapping

import pytest
import yaml

import sequences.main_hand as main_hand
import sequences.sub_hand as sub_hand
from lib.match_state import Court
from lib.sequence.engine import Sequence
from lib.sequence.positions import PositionTable, load_position_table
from sequences.motor_check import MAIN_HOME, SUB_HOME, VALVE_AXES, MotorCheckSequence

_CONFIG_DIR = pathlib.Path(__file__).resolve().parent.parent / "config"


async def _collect(available: Collection[str] | None = None) -> list[dict[str, str]]:
    """全ステップを 1 度ずつ通し、実際に指令された ``{軸: 位置}`` を順に集める。

    サブクラスを作って `move_to` を override するのではなく、**基底クラスの
    `move_to` を差し替える**。`Sequence.__init_subclass__` は `cls.__dict__` しか
    走査しないので、サブクラス化すると親の `@step` が 1 つも引き継がれず
    **ステップ 0 件のまま全テストが緑になる**。インスタンス属性への代入では
    構成による絞り込み (`Sequence.move_to` が持つ) を丸ごと迂回してしまい、
    「存在しない軸へ指令していないこと」を見られない。
    """
    seq = MotorCheckSequence(available_axes=available)
    calls: list[dict[str, str]] = []

    async def _record(
        _self: Sequence, targets: Mapping[str, str], *, timeout: float | None = None
    ) -> None:
        calls.append(dict(targets))

    original = Sequence.move_to
    Sequence.move_to = _record  # type: ignore[method-assign, assignment]
    try:
        assert seq.steps, "ステップが 1 つも無い (収集方法が壊れている)"
        for info in seq.steps:
            await getattr(seq, info.method_name)()
    finally:
        Sequence.move_to = original  # type: ignore[method-assign]
    return calls


def _shipped_table() -> PositionTable:
    return _table_of(sorted(_CONFIG_DIR.glob("*_positions.yaml")))


def _table_of(paths: list[pathlib.Path]) -> PositionTable:
    tables = [
        load_position_table(yaml.safe_load(path.read_text()) or {}, source=path.name)
        for path in paths
    ]
    return PositionTable.merged(tables)


def _main_hand_axes() -> tuple[str, ...]:
    """メインハンドだけを載せた構成の軸。

    `config/bench/main_hand/` は本番の `config/main_hand_positions.yaml` を
    そのまま使い、サブハンド (Damiao DM3520 用 CANable 未接続) を持たない。
    """
    return _table_of([_CONFIG_DIR / "main_hand_positions.yaml"]).axes


class TestShippedConfig:
    async def test_全ての指令が実_config_の位置定数で引ける(self) -> None:
        """軸名・位置名の綴り違いをここで落とす。

        引けない指令が 1 つでもあると、その手前まで機体を動かしてから
        シーケンスが止まる (中途半端な姿勢で残る)。
        """
        table = _shipped_table()

        for targets in await _collect():
            for axis, position in targets.items():
                # 引けなければ PositionLookupError。コート差のある位置も両方見る
                for court in (Court.RED, Court.BLUE):
                    table.raw(axis, position, court=court)

    async def test_実_config_の全アクチュエータ軸を一度は動かす(self) -> None:
        """確認から漏れた軸を作らない。

        漏れていても症状は「その機構だけ試合で初めて動く」なので、
        セッティングタイムには気付けない。
        """
        table = _shipped_table()
        touched = {axis for targets in await _collect() for axis in targets}

        assert set(table.axes) - touched == set()


class TestPairedAxes:
    async def test_左右直結ペアは軸名で指令する(self) -> None:
        """モータ名 (y_axis_r / y_axis_l) で指令してはならない。

        左右が別々の時刻に動くとその場で機構が壊れる。`move_to` は軸名しか
        受け付けないので、モータ名を書くと `PositionLookupError` になる —
        つまりこのテストは「ペア軸をちゃんと動かしていること」を見ている。
        """
        touched = {axis for targets in await _collect() for axis in targets}

        assert "y_axis" in touched
        assert "rotate" in touched
        # ペア軸を構成するモータ名。1 台ずつ動かすとその場で機構が壊れる
        # (wall_f / wall_r は名前が似ているが独立した軸なので対象外)
        assert not ({"y_axis_r", "y_axis_l", "rotate_r", "rotate_l"} & touched)


class TestFinalPosture:
    async def test_最後は両ハンドを初期姿勢へ戻す(self) -> None:
        """途中の姿勢で終わると、操縦者が試合開始前に手動で戻すことになる。"""
        calls = await _collect()

        assert calls[-1] == {**MAIN_HOME, **SUB_HOME}

    async def test_駆動しっぱなしの軸を残さない(self) -> None:
        """コンベア・ポンプ・電磁弁は、回した / 開いたまま終わってはならない。

        最終ステップが初期姿勢へ戻すので通常は成立するが、そこに載っていない軸
        (電磁弁) は各ステップの中で閉じ切る必要がある。
        """
        last: dict[str, str] = {}
        for targets in await _collect():
            last.update(targets)

        assert last["conveyor"] == "stop"
        assert last["pump_vac"] == "stop"
        assert last["pump_blow"] == "stop"
        for axis in VALVE_AXES:
            assert last[axis] == "closed"


class TestHomingComesFirst:
    def test_零点確定が最初のステップ(self) -> None:
        """零点が未確定のまま位置指令を出すと、電源投入位置を原点とみなして
        全ステップが同じだけずれた場所へ動く。"""
        first = MotorCheckSequence("x").steps[0]

        assert "零点" in first.label

    async def test_零点確定は実行口が無ければ素通りする(self) -> None:
        """机上ベンチやセンサ未配線の構成でも、動作確認そのものは試せること。"""
        seq = MotorCheckSequence()
        # bind_homing を呼ばない = 実行口が無い状態
        await seq.home_axes()  # 例外にならなければよい

    async def test_homing_を持つ軸だけを対象にする(self) -> None:
        """`homing:` を書いていない軸を巻き込むと、原点の無い軸を動かし続ける。"""
        table = _shipped_table()
        homing_axes = [name for name in table.axes if table.axis(name).homing is not None]

        # 出荷 config では rotate だけ
        # (y_axis はリミットスイッチ未装着のあいだコメントアウトしてある)
        assert homing_axes == ["rotate"]


class TestStepShape:
    def test_操縦者のトリガー待ちを持たない(self) -> None:
        """20 個以上のアクチュエータを 1 つずつ送らせると操作の手数が確認を上回る。

        代わりに起動前の確認ダイアログで 1 度だけ意思確認する。
        """
        assert all(not info.require_trigger for info in MotorCheckSequence("x").steps)

    @pytest.mark.parametrize("axis", VALVE_AXES)
    async def test_電磁弁は一個ずつ開閉する(self, axis: str) -> None:
        """まとめて開くと、どれが鳴っていないのか聞き分けられない。"""
        opened = [targets for targets in await _collect() if targets.get(axis) == "open"]

        assert len(opened) == 1
        # 同じ指令で他の弁を巻き込んでいないこと
        assert set(opened[0]) == {axis}


class TestConstantsHaveASingleOwner:
    """初期姿勢と電磁弁の軸名を、運用と動作確認で書き写さない。

    書き写すと、機構が変わったときに片方だけ直った状態が作れる。しかも
    動作確認は「自分の写し」で走るので必ず成功し、食い違いは試合で初めて出る。
    """

    def test_メインハンドの初期姿勢は_main_hand_が持つ(self) -> None:
        assert MAIN_HOME is main_hand.HOME

    def test_電磁弁の軸名は_sub_hand_が持つ(self) -> None:
        assert VALVE_AXES is sub_hand.VALVE_AXES

    async def test_メインハンドは同じ初期姿勢へ往復する(self) -> None:
        """往路 (move_to_home) と復路 (return_home) が別の姿勢になってはならない。

        かつては同じ 6 軸の dict がリテラルで 2 回書かれており、片方だけ直せた。
        """
        seq = main_hand.MainHandSequence()
        calls: list[dict[str, str]] = []

        async def _record(targets, *, timeout: float | None = None) -> None:
            calls.append(dict(targets))

        seq.move_to = _record  # type: ignore[method-assign]
        await seq.move_to_home()
        await seq.return_home()

        assert calls == [MAIN_HOME, MAIN_HOME]

    def test_ステップの宣言軸は初期姿勢と電磁弁から導かれる(self) -> None:
        """登録可否の判定材料を呼び出し側で組み直させない。

        かつては `REQUIRED_AXES` という 1 つの定数がステップ表と別の場所に
        あり、ステップが軸を増やしたときに判定だけが古いまま残せた。宣言を
        ステップの隣へ置いた今でも、**宣言の出どころは運用シーケンスの定数**
        でなければならない (書き写すと動作確認だけが古い軸名で通る)。
        """
        declared = {axis for info in MotorCheckSequence("x").steps for axis in info.axes}

        assert declared == {*MAIN_HOME, *SUB_HOME, *VALVE_AXES}


class TestPartialConfiguration:
    """機構が未装着のハンドを外した構成 (`config/bench/main_hand`)。

    **除外は必ず読み取れる形で残す。** 存在しない軸のステップを黙って落とすと、
    本番構成で 1 軸が config から漏れていてもそのステップごと消えて全ステップが
    成功する。症状は「動作確認は通ったのに試合でその軸だけ動かない」だけで、
    確認そのものが意味を失う。
    """

    def test_サブハンド系のステップが登録から外れる(self) -> None:
        seq = MotorCheckSequence(available_axes=_main_hand_axes())
        labels = [info.label for info in seq.steps]

        assert not [label for label in labels if "サブハンド" in label]
        # メインハンドの確認は 1 つも減らない (減らすと実機を確かめられない)
        assert [label for label in labels if "メインハンド" in label] == [
            "メインハンド 初期姿勢へ",
            "メインハンド y 軸 (左右直結ペア)",
            "メインハンド エンドエフェクタ回転 (左右直結ペア)",
            "メインハンド グリッパ",
            "メインハンド 壁 前後",
            "メインハンド コンベア (目視確認)",
        ]

    def test_零点確定は軸を宣言しないので構成に依らず残る(self) -> None:
        """対象を実行時に決めるステップまで消えると、原点が未確定のまま走る。"""
        seq = MotorCheckSequence(available_axes=_main_hand_axes())

        assert "零点" in seq.steps[0].label

    def test_除外したステップと欠けている軸が読める(self) -> None:
        """「機構が未装着だから減っている」のか「config の書き忘れで減っている」の
        かを操縦者が区別できる唯一の材料。"""
        seq = MotorCheckSequence(available_axes=_main_hand_axes())
        excluded = {info.label: info.missing_axes for info in seq.excluded_steps}

        assert excluded == {
            "サブハンド 初期姿勢へ": tuple(sorted(SUB_HOME)),
            "サブハンド アーム関節": ("sub_arm_joint",),
            "サブハンド 前後スライド (Y 方向)": ("sub_y_axis",),
            "サブハンド 昇降": ("sub_lift",),
            "サブハンド 補助ハンド": ("sub_gripper",),
            "サブハンド 電磁弁 6 個 (打音・目視確認)": tuple(sorted(VALVE_AXES)),
            "サブハンド 吸気・排気ポンプ (聴音確認)": ("pump_blow", "pump_vac"),
        }

    def test_本番構成では一つも除外されない(self) -> None:
        """除外が本番でも起きるようなら、それは config の書き忘れである。"""
        seq = MotorCheckSequence(available_axes=_shipped_table().axes)

        assert seq.excluded_steps == ()
        assert len(seq.steps) == len(MotorCheckSequence("x").steps)

    async def test_存在しない軸へは一度も指令しない(self) -> None:
        """絞り込みを各ステップの本体に書くと、書き忘れた 1 行だけが
        `PositionLookupError` で落ちる (しかもその構成でしか症状が出ない)。"""
        available = set(_main_hand_axes())
        touched = {axis for targets in await _collect(available) for axis in targets}

        assert touched <= available

    async def test_最後は存在する軸だけを初期姿勢へ戻す(self) -> None:
        """片方のハンドが不在でも「必ず初期姿勢で終わる」性質は保つ。"""
        available = set(_main_hand_axes())
        calls = await _collect(available)

        assert calls[-1] == {axis: pos for axis, pos in MAIN_HOME.items() if axis in available}
        assert calls[-1] == MAIN_HOME, "メインハンドの初期姿勢が欠けている"

    def test_軸が一本も無ければ指令するステップが残らない(self) -> None:
        """登録しない判断 (`main._wire_motor_check_sequence`) の材料。"""
        seq = MotorCheckSequence(available_axes=[])

        assert not any(info.axes for info in seq.steps)
