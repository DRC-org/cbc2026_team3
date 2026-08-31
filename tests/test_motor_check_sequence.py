"""統合動作確認シーケンス (robots/motor_check.py)。

**実際に出荷する config で成立することを見る。** シーケンスが参照する軸名・位置名は
`config/*_positions.yaml` にしか存在しないので、片方だけ直すと「動作確認を押した
瞬間に PositionLookupError で止まる」形でしか現れない。しかもセッティングタイムに
発覚するため、直す時間が無い。

指令の収集に `move_to` の差し替えを使うのは、CAN もモータも無しで全ステップを
1 度ずつ通すため。到達待ちの実装ではなく **どこへ指令するか** を見たい。
"""

from __future__ import annotations

import pathlib
from collections.abc import Mapping

import pytest
import yaml

import robots.main_hand as main_hand
import robots.sub_hand as sub_hand
from lib.match_state import Court
from lib.sequence.positions import PositionTable, load_position_table
from robots.motor_check import MAIN_HOME, REQUIRED_AXES, SUB_HOME, VALVE_AXES, MotorCheckSequence

_CONFIG_DIR = pathlib.Path(__file__).resolve().parent.parent / "config"


async def _collect() -> list[dict[str, str]]:
    """全ステップを 1 度ずつ通し、`move_to` に渡った指令を順に集める。

    サブクラスを作って `move_to` を override するのではなく、インスタンス属性で
    差し替える。`Sequence.__init_subclass__` は `cls.__dict__` しか走査しないので、
    サブクラス化すると親の `@step` が 1 つも引き継がれず、**ステップ 0 件のまま
    全テストが緑になる**。
    """
    seq = MotorCheckSequence()
    calls: list[dict[str, str]] = []

    async def _record(targets: Mapping[str, str], *, timeout: float | None = None) -> None:
        calls.append(dict(targets))

    seq.move_to = _record  # type: ignore[method-assign]

    assert seq.steps, "ステップが 1 つも無い (収集方法が壊れている)"
    for info in seq.steps:
        await getattr(seq, info.method_name)()
    return calls


def _shipped_table() -> PositionTable:
    tables = [
        load_position_table(yaml.safe_load(path.read_text()) or {}, source=path.name)
        for path in sorted(_CONFIG_DIR.glob("*_positions.yaml"))
    ]
    return PositionTable.merged(tables)


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

        # 出荷 config では y_axis だけ (rotate はハード追加待ちでコメントアウト)
        assert homing_axes == ["y_axis"]


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

    def test_必要な軸は初期姿勢と電磁弁から導かれる(self) -> None:
        """登録可否の判定材料を呼び出し側で組み直させない。"""
        assert {*MAIN_HOME, *SUB_HOME, *VALVE_AXES} == REQUIRED_AXES
