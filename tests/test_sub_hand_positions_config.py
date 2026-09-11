"""同梱の `config/sub_hand_positions.yaml` が「満たすべき関係」を保っているかを見る。

値そのものは機構が付くまで仮値なので固定しない。固定するのは**値どうしの関係**で、
崩れると試合シーケンス (`sequences/sub_hand.py`) が干渉制約を守れなくなる ——
症状は「順序どおりに動いたのに機構が当たる」で、シーケンスからは読めない。
"""

from __future__ import annotations

import pathlib
import re

import pytest
import yaml

from lib.match_state import Court
from lib.motion_guard import AxisReading, GuardViolation, MotionGuard
from lib.sequence.homing import homing_axis_names, homing_order
from lib.sequence.positions import PositionTable, load_position_table

_CONFIG_DIR = pathlib.Path(__file__).resolve().parent.parent / "config"
_YAML_NAME = "sub_hand_positions.yaml"
_FIRMWARE_DIR = _CONFIG_DIR.parent / "firmware" / "servo" / "include"

# 前端スイッチ (動作点 0.0mm) からこれより内側では sub_rotate を回すと干渉する。
_ROTATE_CLEARANCE_MM = 150.0

# 前端スイッチより手前 = 150mm 未満に取る位置。ここからは clear を経由しないと回せない。
# 箱で近いのは一番前の 1 つだけ。残りは 147mm 間隔で後ろへ並ぶので 150mm より遠く、
# 直接回しても当たらない (それでもシーケンスは全箱で clear を経由する)。
_NEAR_FRONT = ("receive", "place_1")

_BOXES = ("place_1", "place_2", "place_3", "place_4")


@pytest.fixture(scope="module")
def table() -> PositionTable:
    return load_position_table(
        yaml.safe_load((_CONFIG_DIR / _YAML_NAME).read_text()), source=_YAML_NAME
    )


def _value(table: PositionTable, axis: str, name: str) -> float:
    return table.raw(axis, name)


def _moves_with_lift_at(table: PositionTable, lift_mm: float) -> bool:
    """昇降がその高さに居るとき、同梱 config で前後の指令が通るか。"""
    guard = table.axis("sub_y_axis").guard
    assert guard is not None
    try:
        MotionGuard(guard).check_interference(
            axis="sub_y_axis",
            delta=-1.0,
            axis_state=lambda _axis: AxisReading(
                value=lift_mm, target=lift_mm, origin_confirmed=True
            ),
        )
    except GuardViolation:
        return False
    return True


# 基板 #2 のスロットは can_id 0x50〜0x54 の昇順で `config.h` の宣言順に対応する。
_BOARD2_CAN_IDS = range(0x50, 0x55)


def _firmware_initial_angles() -> dict[str, float]:
    """`config.h` の基板 #2 の `initialAngleDeg` を モータ名 -> 角 で返す。

    ファームは基板とスロット番号しか知らず、モータ名を知っているのは
    `config/sub_hand.yaml` の `can_id` だけなので、両方を読まないと対応が付かない。
    """
    source = (_FIRMWARE_DIR / "config.h").read_text()
    board = re.search(r"\{2,\s*\{(.*?)\}\}", source, re.S)
    assert board is not None, "config.h に基板 #2 の宣言が無い"
    angles = [float(m) for m in re.findall(r"SlotRole::Servo,\s*\d+,\s*([\d.]+)f", board.group(1))]
    assert len(angles) == len(_BOARD2_CAN_IDS), f"基板 #2 のスロットが 5 つでない: {angles}"

    motors = yaml.safe_load((_CONFIG_DIR / "sub_hand.yaml").read_text())["motors"]
    by_can_id = {m["can_id"]: name for name, m in motors.items() if m["can_id"] in _BOARD2_CAN_IDS}
    assert len(by_can_id) == len(_BOARD2_CAN_IDS), f"0x50〜0x54 のモータが 5 台でない: {by_can_id}"

    return {by_can_id[can_id]: angle for can_id, angle in zip(_BOARD2_CAN_IDS, angles, strict=True)}


class TestSubYAxis:
    def test_位置は仕様の名前が揃っている(self, table: PositionTable) -> None:
        assert set(table.names("sub_y_axis")) == {
            "retracted",
            "home",
            "clear",
            "receive",
            *_BOXES,
        }

    def test_clear_は前端スイッチから_150mm_以上離れている(self, table: PositionTable) -> None:
        # ここでだけ sub_rotate を回してよい。近付けると回した機構が前端側に当たる
        assert abs(_value(table, "sub_y_axis", "clear")) >= _ROTATE_CLEARANCE_MM

    @pytest.mark.parametrize("name", _NEAR_FRONT)
    def test_棚と箱は前端スイッチから_150mm_未満にある(
        self, table: PositionTable, name: str
    ) -> None:
        # 150mm 以上まで下がると「clear を経由しないと回せない」が値として崩れ、
        # 回転可能位置へ後退するステップが意味を失う
        assert abs(_value(table, "sub_y_axis", name)) < _ROTATE_CLEARANCE_MM

    def test_retracted_が一番後ろである(self, table: PositionTable) -> None:
        others = [
            _value(table, "sub_y_axis", name)
            for name in table.names("sub_y_axis")
            if name != "retracted"
        ]

        assert _value(table, "sub_y_axis", "retracted") < min(others)

    def test_箱_4_箇所は前から順に後ろへ並ぶ(self, table: PositionTable) -> None:
        # 番号と並びが食い違うと、シーケンスが箱を跨いで前後する (順序はステップの前提)。
        # 後ろほど値が小さい (前端が 0.0 で後ろが負)。
        boxes = [_value(table, "sub_y_axis", name) for name in _BOXES]

        assert boxes == sorted(boxes, reverse=True)
        assert len(set(boxes)) == 4


class TestSubLift:
    def test_位置は仕様の名前が揃っている(self, table: PositionTable) -> None:
        assert set(table.names("sub_lift")) == {"top", "pick", "lifted", "above_box", "place"}

    def test_pick_は_top_より上に行かない(self, table: PositionTable) -> None:
        # + が下なので「下」は値が大きい側。符号を取り違えると移動高さより上へ逃げる
        assert _value(table, "sub_lift", "pick") >= _value(table, "sub_lift", "top")

    def test_箱へ下ろす手前は箱へ下ろす高さより上にある(self, table: PositionTable) -> None:
        # + が下。ここが逆だと、縁で止めるつもりの段が先に箱の中まで下りる
        assert _value(table, "sub_lift", "above_box") < _value(table, "sub_lift", "place")


class TestServoAxes:
    @pytest.mark.parametrize(
        ("axis", "names"),
        [
            ("sub_rotate", {"receive", "carry"}),
            ("sub_pitch", {"open", "close"}),
            ("sub_offset", {"open", "close"}),
        ],
    )
    def test_位置は仕様の名前が揃っている(
        self, table: PositionTable, axis: str, names: set[str]
    ) -> None:
        assert set(table.names(axis)) == names

    @pytest.mark.parametrize(
        ("axis", "name"),
        [("sub_rotate", "receive"), ("sub_pitch", "open"), ("sub_offset", "open")],
    )
    def test_初期姿勢はファームの_initialAngleDeg_と揃っている(
        self, table: PositionTable, axis: str, name: str
    ) -> None:
        """基板 #2 は通電と瞬断からの再起動のたび `initialAngleDeg` へ駆動する。

        シーケンスの初期姿勢と食い違うと、電源を入れ直すたびに機構が別の姿勢へ飛ぶ。
        **左右ペアはモータ 1 台ずつ突き合わせる** —— 軸の値だけを見ると、
        折り返し点 (`offset`) がずれていても気付けない。
        """
        initial = _firmware_initial_angles()

        for motor, command in table.commands(axis, name).items():
            assert command == pytest.approx(initial[motor]), (
                f"{axis}.{name} の {motor} が firmware/servo/include/config.h の"
                f" initialAngleDeg と違う (config {command} / ファーム {initial[motor]})"
            )


class TestEveryPosition:
    # `manual` の可動範囲に収まっているかはここでは見ない。範囲外の位置定数は
    # `_check_manual_range` が読み込みの時点で起動拒否にするので、この fixture が
    # そもそも組めない (同じ判定を 2 箇所に書かない)。

    def test_直動_2_軸はスイッチの動作点そのものを目標にしない(self, table: PositionTable) -> None:
        """0.0 は前端・下端スイッチの動作点。tolerance 1.0mm は動作点を 1mm 越えた場所も

        到達として受け、ON 区間の内側からの指令は可動端インターロックが拒否する。
        """
        at_origin = [
            f"{axis}.{name}"
            for axis in ("sub_y_axis", "sub_lift")
            for name in table.names(axis)
            if _value(table, axis, name) == 0.0
        ]

        assert at_origin == []

    def test_ベンチの位置定数もすべて読める(self) -> None:
        """干渉条件の起動拒否を足しても、机上ベンチの一式が読めなくなっていないこと。

        `tests/test_config_schema.py` の `test_bench_config_set_loads` は
        **そのベンチの robot yaml から辿れる 1 枚**しか開かない。ここは
        `config/bench/**` に置いてある位置定数を名前で拾うので、robot yaml から
        辿れない 1 枚が混じっても落ちる。
        """
        shipped = sorted(_CONFIG_DIR.glob("bench/*/*_positions.yaml"))

        assert shipped, "config/bench にベンチ用の位置定数が 1 枚もありません"
        for path in shipped:
            load_position_table(yaml.safe_load(path.read_text()), source=str(path))

    def test_位置定数はコート別に分岐していない(self, table: PositionTable) -> None:
        """コートで変わるのは `sub_lift` の mm↔rad 換算 (scale の符号) だけである。

        `positions` へコートの分岐を書くと、mm の座標系が片方のコートだけ別物になる。
        """
        for axis in table.axes:
            for name in table.names(axis):
                # コート別に書いてあると court 無しの raw が PositionLookupError になる
                value = table.raw(axis, name)
                assert value == table.raw(axis, name, court=Court.RED)
                assert value == table.raw(axis, name, court=Court.BLUE)


class TestInterferenceDeclaration:
    """`guard.requires` / `guard.not_with` の宣言が、守るべき制約と噛み合っているか。

    宣言は位置名で書き、数値の区間は読み込み時に解決される。**150.0 はこのファイルの
    `_ROTATE_CLEARANCE_MM` 1 箇所にしか無く**、それが `clear` を縛り、`clear` が
    解決後の区間を縛る。ここで見るのは、その連鎖が切れていないこと。
    """

    def test_前後は昇降の高さに関わらず動かせる(self, table: PositionTable) -> None:
        """昇降の高さは条件にしない (2026-09-11)。

        当たらないのは `pick`〜`top` のあたりだが、零点が確定していない昇降を条件に
        すると前後が 1mm も動かせず、手で寄せて測る作業が回らない。守りは零点確定の
        並び (`TestHomingOrder`) と操縦者の目視へ移した。
        """
        guard = table.axis("sub_y_axis").guard
        assert guard is not None and guard.requires == ()
        for lift_mm in (0.0, -10.0, -76.0, -140.0):
            assert _moves_with_lift_at(table, lift_mm)

    def test_ピッチとオフセットは同じ指令で動かせない(self, table: PositionTable) -> None:
        """yaml には片側だけ書く。読み込み時に対称化されることを両側で見る。"""
        pitch = table.axis("sub_pitch").guard
        offset = table.axis("sub_offset").guard

        assert pitch is not None and pitch.not_with == ("sub_offset",)
        assert offset is not None and offset.not_with == ("sub_pitch",)


_BENCH_YAML = "bench/sub_hand_homing/sub_hand_positions.yaml"


def _load(relative: str) -> PositionTable:
    path = _CONFIG_DIR / relative
    return load_position_table(yaml.safe_load(path.read_text()), source=path.name)


@pytest.mark.parametrize("relative", [_YAML_NAME, _BENCH_YAML])
class TestHomingOrder:
    """零点確定は「昇降 → 自分で移動高さへ退避 → 前後」の順でしか通らない。

    `guard.requires` を外した (前後は昇降の高さに関わらず動かせる) ので、順序を
    決めるのは **yaml の並び**、高さを作るのは **`sub_lift` の `retreat_position`**
    になった。どちらかが抜けると、確定しただけの昇降 (下端のすぐ上) のまま前後を
    探索して機構が当たる。ベンチは mm の値が本番と違うだけで、同じ形が要る。
    """

    def test_昇降を先に確定する(self, relative: str) -> None:
        table = _load(relative)
        axes = homing_axis_names(table)

        # 並べ替える宣言が無いので、yaml の並びがそのまま順になる
        assert axes.index("sub_lift") < axes.index("sub_y_axis")
        assert homing_order(table, axes).index("sub_lift") < homing_order(table, axes).index(
            "sub_y_axis"
        )

    def test_昇降は確定した直後に前後を走らせる高さへ退避する(self, relative: str) -> None:
        table = _load(relative)
        homing = table.axis("sub_lift").homing

        assert homing is not None and homing.retreat_position == "pick"

    def test_零点確定を終えただけの昇降は移動高さに居ない(self, relative: str) -> None:
        """**退避する段が要ることの根拠。** ここが移動高さなら退避は要らない。"""
        table = _load(relative)
        homing = table.axis("sub_lift").homing
        assert homing is not None and homing.release_distance is not None
        # 探索は + 方向 (下端) へ進み、原点確定の後に逆向きへ release_distance 離脱する
        left_at = -homing.direction * homing.release_distance

        assert abs(left_at - table.raw("sub_lift", "pick")) > table.axis("sub_lift").tolerance


class TestLinearAxisTimeout:
    """直動 2 軸の `timeout_s` は起動時の検算に載っていなければならない。

    速度をドライバ内蔵の位置ループが決める軸は `motion` を持たないので、`min_speed` を
    書かない限り `timeout_s` は誰にも咎められない。症状は「零点確定の寄せが毎回
    時間切れで落ちる」で、機構にもモータにも異常が無い。
    """

    _LINEAR_AXES = ("sub_y_axis", "sub_lift")

    def test_直動_2_軸は_min_speed_で検算に載っている(self, table: PositionTable) -> None:
        for axis in self._LINEAR_AXES:
            assert table.axis(axis).min_speed is not None, axis

    @pytest.mark.parametrize("axis", _LINEAR_AXES)
    def test_manual_の全幅に足りない_timeout_s_は起動を拒否する(self, axis: str) -> None:
        config = yaml.safe_load((_CONFIG_DIR / _YAML_NAME).read_text())
        config["axes"][axis]["timeout_s"] = 4.0

        with pytest.raises(ValueError, match=rf"axes\.{axis} は min_speed .*timeout_s \(4\.0\)"):
            load_position_table(config, source=_YAML_NAME)
