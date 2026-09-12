"""サーボの回転をリンクで直線に変える機構の換算。

サブハンドのピッチは**スライダクランク**で、サーボに付いたクランク (`crank`) と
連結棒 (`rod`) が、サーボ軸から先端までの距離 `x` を作る::

    x(θ) = crank·cosθ + √(rod² - crank²·sin²θ)

左右 2 台が `span` だけ離れて向かい合うので、**先端どうしの隙間**は
`span - 左 - 右` になる。隙間と中心のずれを指定すれば左右の距離が決まり、
上の式を逆に解いてサーボ角が出る。

**隙間を負にできない形にしてある。** 左右の和が `span` を超えると機構が押し合って
壊れる (2026-09-11 に棒を 45/70 から 65/90 へ伸ばしたとき、同じ角のままだと
80mm 押し合う計算になった)。和を直接指令できる口を作らず、隙間と中心という
「押し合いようのない 2 つ」で表すのが歯止めである。
"""

from __future__ import annotations

import math
from dataclasses import dataclass


class LinkageError(ValueError):
    """指定した隙間・中心では機構が届かない (または押し合う)。"""


@dataclass(frozen=True)
class Linkage:
    """左右 1 対のスライダクランク。長さは mm、角は deg。"""

    #: サーボに付く短いほうの棒
    crank: float
    #: クランクと先端をつなぐ棒
    rod: float
    #: 左右のサーボ軸どうしの距離
    span: float
    #: クランク角 0deg (最も伸びた姿勢) のときのサーボ角
    servo_zero: float

    def __post_init__(self) -> None:
        if not (0.0 < self.crank < self.rod):
            raise LinkageError(
                f"crank ({self.crank}) は rod ({self.rod}) より短い正の値である必要があります"
            )
        if self.span <= 0.0:
            raise LinkageError(f"span ({self.span}) は正の値である必要があります")

    @property
    def min_reach(self) -> float:
        """片側が一番縮んだときの距離 (クランク角 180deg)。"""
        return self.rod - self.crank

    @property
    def max_reach(self) -> float:
        """片側が一番伸びたときの距離 (クランク角 0deg)。"""
        return self.rod + self.crank

    @property
    def max_gap(self) -> float:
        """左右を縮めきったときの隙間。これより広くはできない。"""
        return self.span - 2.0 * self.min_reach

    def reach_from_crank(self, crank_deg: float) -> float:
        """クランク角 (0deg が伸びきり) から片側の距離を出す。"""
        theta = math.radians(crank_deg)
        inner = self.rod**2 - (self.crank * math.sin(theta)) ** 2
        return self.crank * math.cos(theta) + math.sqrt(max(inner, 0.0))

    def crank_angle(self, reach_mm: float) -> float:
        """片側の距離からクランク角を出す。**解は 2 つあるが 0〜180deg の側を返す。**

        クランク角 θ と -θ は同じ距離を作る。片側だけ折り返した姿勢は左右で
        機構の向きが変わってしまうので、常に同じ側を使う。
        """
        if not (self.min_reach - 1e-9 <= reach_mm <= self.max_reach + 1e-9):
            raise LinkageError(
                f"片側の距離 {reach_mm:.3g}mm は可動域 "
                f"{self.min_reach:.3g}〜{self.max_reach:.3g}mm の外です"
            )
        cos_theta = (reach_mm**2 - self.rod**2 + self.crank**2) / (2.0 * reach_mm * self.crank)
        return math.degrees(math.acos(max(-1.0, min(1.0, cos_theta))))

    def reach(self, servo_deg: float) -> float:
        """サーボ角から片側の距離を出す。"""
        return self.reach_from_crank(servo_deg - self.servo_zero)

    def servo_angle(self, reach_mm: float) -> float:
        """片側の距離からサーボ角を出す。"""
        return self.crank_angle(reach_mm) + self.servo_zero

    def reaches(self, gap_mm: float, center_mm: float = 0.0) -> tuple[float, float]:
        """(左の距離, 右の距離)。`center_mm` は**右へ**ずらす量。

        隙間が負 (押し合い) はここで弾く。左右どちらかが可動域を外れるのも弾く。
        """
        if gap_mm < 0.0:
            raise LinkageError(f"隙間 {gap_mm:.3g}mm が負です。左右が {-gap_mm:.3g}mm 押し合います")
        if gap_mm > self.max_gap + 1e-9:
            raise LinkageError(f"隙間 {gap_mm:.3g}mm は最大 {self.max_gap:.3g}mm を超えています")
        half = (self.span - gap_mm) / 2.0
        return half + center_mm, half - center_mm

    def angles(self, gap_mm: float, center_mm: float = 0.0) -> tuple[float, float]:
        """(左のサーボ角, 右のサーボ角)。押し合う指定はここで弾かれる。"""
        left, right = self.reaches(gap_mm, center_mm)
        return self.servo_angle(left), self.servo_angle(right)

    def center_limit(self, gap_mm: float) -> float:
        """その隙間のとき中心をずらせる量 (±)。可動域から決まる。"""
        half = (self.span - gap_mm) / 2.0
        return min(self.max_reach - half, half - self.min_reach)
