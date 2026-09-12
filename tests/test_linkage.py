"""サーボの回転をリンクで直線に変える機構の換算。

**機体を動かす数値なので固定する。** ここがずれると、左右が押し合って機構が壊れるか、
閉じ切らずにワークを落とす。実測は 2026-09-11 のサブハンド (クランク 65mm /
ロッド 90mm / サーボ軸間 230mm)。
"""

from __future__ import annotations

import math

import pytest

from lib.linkage import Linkage, LinkageError

# 2026-09-11 の実機。棒を 45/70 から 65/90 へ伸ばした後
_SUB_PITCH = Linkage(crank=65.0, rod=90.0, span=230.0, servo_zero=80.0)


class TestGeometry:
    def test_可動域は_rod_プラスマイナス_crank(self) -> None:
        assert _SUB_PITCH.min_reach == pytest.approx(25.0)
        assert _SUB_PITCH.max_reach == pytest.approx(155.0)

    def test_閉じ切りのサーボ角(self) -> None:
        """隙間 0 は左右とも 115mm。棒を伸ばす前の 80deg のままだと 80mm 押し合う。"""
        left, right = _SUB_PITCH.angles(0.0)

        assert left == pytest.approx(131.29, abs=0.01)
        assert right == pytest.approx(left)

    @pytest.mark.parametrize("servo_deg", [80.0, 110.0, 131.29, 180.0, 230.0, 260.0])
    def test_角から距離_距離から角が往復する(self, servo_deg: float) -> None:
        reach = _SUB_PITCH.reach(servo_deg)

        assert _SUB_PITCH.servo_angle(reach) == pytest.approx(servo_deg, abs=1e-6)

    def test_式どおりの距離を返す(self) -> None:
        crank, rod = _SUB_PITCH.crank, _SUB_PITCH.rod
        for servo_deg in (90.0, 140.0, 200.0):
            theta = math.radians(servo_deg - _SUB_PITCH.servo_zero)
            expected = crank * math.cos(theta) + math.sqrt(
                rod**2 - (crank * math.sin(theta)) ** 2
            )
            assert _SUB_PITCH.reach(servo_deg) == pytest.approx(expected)


class TestCenterOffset:
    """中心をずらしても左右の和は変わらない —— だから押し合いようがない。"""

    @pytest.mark.parametrize("center", [-40.0, -20.0, 0.0, 20.0, 40.0])
    def test_中心をずらしても隙間は変わらない(self, center: float) -> None:
        left, right = _SUB_PITCH.reaches(0.0, center)

        assert left + right == pytest.approx(_SUB_PITCH.span)

    def test_右へずらすと左が伸びる(self) -> None:
        left, right = _SUB_PITCH.reaches(0.0, 20.0)

        assert left == pytest.approx(135.0)
        assert right == pytest.approx(95.0)

    def test_ずらせる量は可動域から決まる(self) -> None:
        assert _SUB_PITCH.center_limit(0.0) == pytest.approx(40.0)
        # 縮めきると左右とも最短で 1mm も動かせない
        assert _SUB_PITCH.center_limit(_SUB_PITCH.max_gap) == pytest.approx(0.0)
        # 一番ずらせるのは片側が可動域の真ん中に来るとき (両端の死点から遠い)
        middle_gap = _SUB_PITCH.span - (_SUB_PITCH.min_reach + _SUB_PITCH.max_reach)
        assert _SUB_PITCH.center_limit(middle_gap) == pytest.approx(_SUB_PITCH.crank)


class TestRejects:
    """**押し合う指定は必ず送出する。** 通すと機構がその場で壊れる。"""

    def test_隙間が負なら拒む(self) -> None:
        with pytest.raises(LinkageError, match="押し合"):
            _SUB_PITCH.angles(-0.1)

    def test_隙間が最大を超えたら拒む(self) -> None:
        with pytest.raises(LinkageError):
            _SUB_PITCH.angles(_SUB_PITCH.max_gap + 0.1)

    def test_ずらしすぎたら拒む(self) -> None:
        with pytest.raises(LinkageError, match="可動域"):
            _SUB_PITCH.angles(0.0, _SUB_PITCH.center_limit(0.0) + 1.0)

    def test_crank_が_rod_以上なら組み立てを拒む(self) -> None:
        with pytest.raises(LinkageError):
            Linkage(crank=90.0, rod=65.0, span=230.0, servo_zero=80.0)
