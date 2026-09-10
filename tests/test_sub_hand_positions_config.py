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
from lib.motion_guard import AxisReading, GuardViolation, MotionGuard, RequiredRange
from lib.sequence.homing import homing_axis_names, homing_order
from lib.sequence.positions import PositionTable, load_position_table

_CONFIG_DIR = pathlib.Path(__file__).resolve().parent.parent / "config"
_YAML_NAME = "sub_hand_positions.yaml"
_FIRMWARE_DIR = _CONFIG_DIR.parent / "firmware" / "servo" / "include"

# 前端スイッチ (動作点 0.0mm) からこれより内側では sub_rotate を回すと干渉する。
_ROTATE_CLEARANCE_MM = 150.0

# 吸着は移動高さから 10mm だけ降りて行う。+ が下なので pick の値は top より大きい。
_PICK_DROP_MM = 10.0

# 前端スイッチより手前 = 150mm 未満に取る位置。ここからは clear を経由しないと回せない。
_NEAR_FRONT = ("receive", "place_1", "place_2", "place_3", "place_4")


@pytest.fixture(scope="module")
def table() -> PositionTable:
    return load_position_table(
        yaml.safe_load((_CONFIG_DIR / _YAML_NAME).read_text()), source=_YAML_NAME
    )


def _value(table: PositionTable, axis: str, name: str) -> float:
    return table.raw(axis, name)


def _requirement(table: PositionTable, axis: str) -> RequiredRange:
    guard = table.axis(axis).guard
    assert guard is not None
    (required,) = guard.requires
    return required


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
            "clear",
            *_NEAR_FRONT,
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

    def test_箱_4_箇所は互いに違う位置である(self, table: PositionTable) -> None:
        boxes = [_value(table, "sub_y_axis", f"place_{n}") for n in range(1, 5)]

        assert len(set(boxes)) == 4


class TestSubLift:
    def test_位置は仕様の名前が揃っている(self, table: PositionTable) -> None:
        assert set(table.names("sub_lift")) == {"top", "pick", "place"}

    def test_pick_は_top_のちょうど_10mm_下(self, table: PositionTable) -> None:
        # + が下なので「下」は値が大きい側。符号を取り違えると 10mm 上へ逃げる
        assert _value(table, "sub_lift", "pick") == pytest.approx(
            _value(table, "sub_lift", "top") + _PICK_DROP_MM
        )

    def test_place_は_pick_より下にある(self, table: PositionTable) -> None:
        assert _value(table, "sub_lift", "place") > _value(table, "sub_lift", "pick")


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

    def test_前後に動かしてよいのは昇降が移動高さのときだけ(self, table: PositionTable) -> None:
        required = _requirement(table, "sub_y_axis")
        tolerance = table.axis("sub_lift").tolerance
        top = _value(table, "sub_lift", "top")

        assert required.axis == "sub_lift"
        assert tolerance is not None
        # 区間を tolerance ぶん広げないと、top へ許容差の内側で止まった実測が
        # 区間の外になり、次に前後へ動かす段が会場で拒否される
        assert required.low == pytest.approx(top - tolerance)
        assert required.high == pytest.approx(top + tolerance)

    @pytest.mark.parametrize("name", ["pick", "place"])
    def test_移動高さより下では前後に動かせない(self, table: PositionTable, name: str) -> None:
        required = _requirement(table, "sub_y_axis")
        value = _value(table, "sub_lift", name)

        assert not required.low <= value <= required.high

    def test_回転してよい区間は前端から_150mm_以上離れている(self, table: PositionTable) -> None:
        # 区間の前端寄りの縁 (high) がこの余裕の内側に入ると、そこで回した機構が当たる
        required = _requirement(table, "sub_rotate")

        assert required.axis == "sub_y_axis"
        assert abs(required.high) >= _ROTATE_CLEARANCE_MM

    def test_回転してよい区間は_clear_を含む(self, table: PositionTable) -> None:
        required = _requirement(table, "sub_rotate")
        clear = _value(table, "sub_y_axis", "clear")

        assert required.low <= clear <= required.high

    @pytest.mark.parametrize("name", _NEAR_FRONT)
    def test_回転してよい区間は前端寄りの位置を全部除外する(
        self, table: PositionTable, name: str
    ) -> None:
        # between の端を書き間違える (retracted〜receive など) と、ここが通ってしまう
        required = _requirement(table, "sub_rotate")
        value = _value(table, "sub_y_axis", name)

        assert not required.low <= value <= required.high

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
    """零点確定は「昇降 → top へ寄せる → 前後」の順でしか通らない。**本番とベンチの両方。**

    `release_distance` ぶん離脱して終わるので、確定しただけの昇降は下端のすぐ上に
    居る。`requires` を入れた以上、そこから前後軸を探索する手順は拒否される ——
    拒否は誤動作ではなく、**昇降が下がったまま前後に走っていた**ことの露見である。
    ベンチは mm の値が本番と違うだけで、同じ形が成り立っていなければならない。
    """

    def test_寄せ先は昇降の移動高さである(self, relative: str) -> None:
        table = _load(relative)

        assert table.homing_prerequisites() == {"sub_lift": "top"}

    def test_参照される軸を先に確定する(self, relative: str) -> None:
        table = _load(relative)
        axes = homing_axis_names(table)

        assert homing_order(table, axes).index("sub_lift") < homing_order(table, axes).index(
            "sub_y_axis"
        )
        # 入力の並びに関係なく決まる (yaml の並べ替えで手順が変わってはならない)
        assert homing_order(table, list(reversed(axes)))[0] == "sub_lift"

    def test_零点確定を終えた昇降は区間の外に居る(self, relative: str) -> None:
        """**寄せる段が要ることの根拠。** ここが中なら move_to は 1 通も出さない。"""
        table = _load(relative)
        homing = table.axis("sub_lift").homing
        assert homing is not None and homing.release_distance is not None
        # 探索は + 方向 (下端) へ進み、原点確定の後に逆向きへ release_distance 離脱する
        left_at = -homing.direction * homing.release_distance

        assert not _requirement(table, "sub_y_axis").contains(left_at)

    def test_移動高さへ寄せれば区間の中に入る(self, relative: str) -> None:
        table = _load(relative)

        assert _requirement(table, "sub_y_axis").contains(table.raw("sub_lift", "top"))


_DOC_DIR = _CONFIG_DIR.parent / "docs"


def _left_after_homing(table: PositionTable, axis: str) -> float:
    """零点確定を終えた軸が居る位置 [mm]。原点で当ててから離脱したぶんだけ戻る。"""
    homing = table.axis(axis).homing
    assert homing is not None and homing.release_distance is not None
    return -homing.direction * homing.release_distance


def _rejection(table: PositionTable) -> str:
    """同梱 config そのままで `sub_y_axis` を動かしたときの拒否文面。"""
    guard = table.axis("sub_y_axis").guard
    assert guard is not None
    left_at = _left_after_homing(table, "sub_lift")

    with pytest.raises(GuardViolation) as exc:
        MotionGuard(guard).check_interference(
            axis="sub_y_axis",
            delta=-1.0,
            axis_state=lambda _axis: AxisReading(value=left_at, target=None),
        )
    return str(exc.value)


class TestVenueCardNumbers:
    """会場カードに書き写した数値が、実際の拒否文面と一致しているか。

    **会場で読むのは文書の側である。** 文面は `config/sub_hand_positions.yaml` から
    導かれるので、`top` / `tolerance` / `release_distance` を変えると文面が変わる。
    ここが無いと、**文書の数値だけが黙って古くなる** (症状は「カードのとおりに
    寄せたのに拒否が消えない」で、会場でしか出ない)。
    """

    def test_拒否の文面は同梱_config_から導かれる(self, table: PositionTable) -> None:
        required = _requirement(table, "sub_y_axis")
        message = _rejection(table)

        assert f"[{required.low:.4g}, {required.high:.4g}]{required.unit}" in message
        assert f"実測 {_left_after_homing(table, 'sub_lift'):.4g}{required.unit}" in message
        assert required.label in message

    def test_会場カードが文面と同じ数値を書いている(self, table: PositionTable) -> None:
        text = (_DOC_DIR / "venue_recovery.md").read_text()

        # 手動で寄せる先と、零点確定が離脱する量。この 2 つで手が動く
        assert f"{table.raw('sub_lift', 'top'):g}mm" in text
        assert f"{abs(_left_after_homing(table, 'sub_lift')):g}mm" in text
