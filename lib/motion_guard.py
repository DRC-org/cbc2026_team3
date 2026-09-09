"""軸へ指令を出してよいかの判断。**判断だけを持ち、送信も状態も持たない。**

`lib/axis_sync.py` と同じ最下位層で、上位モジュールを import してはならない。
判断がここに閉じているのは、手動操縦・シーケンス・零点確定の 3 経路が同じ
`AxisHandle` を通るのに対し、**層ごとに書き写すと片方だけ緩んだ状態が作れる**ため
(左右ペアの偏差判定を `SyncGroup.violation()` へ一本化しているのと同じ理由)。

守るのは 4 つで、どれも 2026-09-09 に実機で踏んだ事故に対応する:

1. **可動端のインターロック** —— リミットスイッチが押されている向きへは、それ以上
   指令を出さない。**逆向き (離れる向き) は必ず通す** —— 塞ぐと機構端に張り付いた
   軸を手動でも戻せなくなり、退避路としての手動操縦が成立しなくなる。零点確定の
   離脱段もこの向きを使う
2. **1 指令の跳躍量** —— 実測位置から `max_step` を超えて離れた目標を拒む。
   スケールや固定小数点レンジの取り違えは「桁が変わった目標値」として現れるので、
   **比が分からなくてもここで止まる**。実機では `p_max` の食い違いで位置が 80 倍に
   読め、その値が保持目標として書かれて機構がスイッチを踏み越えた
3. **逆端への到達** —— 探索の向きを取り違えると、狙った端ではなく反対の端へ進む。
   1 の帰結として反対端のスイッチで止まるので、**押し込む前に止まって理由が出る**
4. **急なトルク** —— 何かに当たったことは、位置が進まないことより先にトルクに出る。
   `stall_torque` を超えたら止める

**センサの途絶 (STALE) をここで扱わないのは意図的**である。「押されていない」と
「読めていない」は別の事実で、後者を可動端の判断に混ぜると**配線が抜けたセンサが
「押されていない = 進んでよい」に化ける**。読めていないセンサは `active=None` として
渡され、インターロックは**押されているものと同じ扱い (進ませない)** にする ——
安全側は「止まる」であって「進む」ではない。
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["GuardViolation", "LimitSpec", "MotionGuard", "MotionGuardSpec"]


@dataclass(frozen=True)
class LimitSpec:
    """軸の両端に居るリミットセンサの名前。

    **どちらも省略できる。** 片端にしかスイッチが無い機構は普通にある
    (サブハンド前後は前端が原点で、後端は報告のみだった)。書かなかった側は
    インターロックが掛からないので、書き忘れは「守られていない端」として残る。
    そのため `axes.<軸>.homing.sensor` を書いた軸は、少なくともその 1 本が
    どちらの端かを宣言していることになる。
    """

    #: + 方向の端に居るセンサ名 (値が増える向きに進むと当たる)
    plus: str | None = None
    #: - 方向の端に居るセンサ名
    minus: str | None = None

    def sensor_for(self, delta: float) -> str | None:
        """`delta` の向きに進むとき、当たりうる端のセンサ名。"""
        if delta > 0.0:
            return self.plus
        if delta < 0.0:
            return self.minus
        return None


@dataclass(frozen=True)
class MotionGuardSpec:
    """1 軸ぶんの歯止めの宣言。**軸の機構的性質**なので位置定数と同居する。

    どの項目も省略できるが、**省略は「その守りが無い」ことを意味する**ので、
    機構が付いた軸には必ず書くこと。既定値で埋めないのは、埋めた値が効いている
    のか書き忘れなのかがコードから読めなくなるため (`HealthThresholds` や
    `homing.search_distance` と同じ方針)。
    """

    limits: LimitSpec | None = None
    #: 1 指令で実測位置から離れてよい最大量 [軸の unit]。None なら跳躍を見ない
    max_step: float | None = None
    #: これを超えたら止める絶対トルク [Nm]。None ならトルクを見ない
    stall_torque: float | None = None

    def __post_init__(self) -> None:
        if self.max_step is not None and self.max_step <= 0.0:
            raise ValueError(f"max_step は正の値: {self.max_step!r}")
        if self.stall_torque is not None and self.stall_torque <= 0.0:
            raise ValueError(f"stall_torque は正の値: {self.stall_torque!r}")


class GuardViolation(RuntimeError):
    """歯止めに掛かって指令を出さなかった。

    **握り潰してはならない。** 掛かるのは「配線・スケール・向きのどれかが
    食い違っている」ときだけで、そのまま進めれば機構が壊れる。
    """


class MotionGuard:
    """指令を出す直前の判断。**状態を持たない。**

    センサの状態は呼び出しのたびに注入で受ける (`sensor_active`)。ここで保持すると
    「いつの時点のセンサか」が判断の中に隠れ、`AxisHandle` を作り直すたびに古い値が
    紛れ込む。三値 (`True` / `False` / `None`) をそのまま受けるのは、**`None`
    (読めていない) を `False` (押されていない) へ丸めない**ため。
    """

    def __init__(self, spec: MotionGuardSpec) -> None:
        self._spec = spec

    def check_command(
        self,
        *,
        axis: str,
        current: float,
        target: float,
        unit: str,
        sensor_active: object,
    ) -> None:
        """`current` から `target` へ動かしてよいか。駄目なら送出する。

        Args:
            axis: 軸名 (メッセージ用)
            current: 実測位置 [軸の unit]
            target: 出そうとしている目標 [軸の unit]
            unit: 人間の単位 (メッセージ用)
            sensor_active: 進む向きの端のセンサの状態を返す呼び出し可能オブジェクト
                (`Callable[[str], bool | None]`)。`None` は「読めていない」

        Raises:
            GuardViolation: 跳躍量を超えた / 進む向きの端が押されている
                (または読めていない)
        """
        delta = target - current
        self._check_jump(axis, delta, unit)
        self.check_limit(axis=axis, delta=delta, sensor_active=sensor_active)

    def _check_jump(self, axis: str, delta: float, unit: str) -> None:
        limit = self._spec.max_step
        if limit is None or abs(delta) <= limit:
            return
        raise GuardViolation(
            f"軸 '{axis}' へ 1 度に {abs(delta):.3g}{unit} 動かす指令が出ました"
            f" (上限 {limit}{unit})。**スケールか固定小数点レンジの取り違えを疑うこと** ——"
            " 位置が桁ごとずれて読めていると、保持目標がそのまま桁の違う位置を指し、"
            "機構が可動端まで走ります"
        )

    def check_limit(self, *, axis: str, delta: float, sensor_active: object) -> None:
        """`delta` の向きへ進んでよいか。駄目なら送出する。

        **指令を出す前 (`check_command`) と移動中 (`LimitMonitor`) の両方がここを呼ぶ。**
        監視側へ書き写すと、片方だけが `None` (読めていない) を素通りさせる状態が作れる。

        Raises:
            GuardViolation: 進む向きの端が押されている (または読めていない)
        """
        limits = self._spec.limits
        if limits is None or delta == 0.0:
            return
        sensor = limits.sensor_for(delta)
        if sensor is None:
            return
        state = sensor_active(sensor)  # type: ignore[operator]
        if state is False:
            return
        if state is None:
            raise GuardViolation(
                f"軸 '{axis}' の可動端センサ '{sensor}' が読めていないため、"
                "その向きへは動かしません (配線・基板の電源・デバイス ID を確認してください)。"
                "**読めていないことを「押されていない」と読み替えてはならない**ので、"
                "安全側に倒しています"
            )
        raise GuardViolation(
            f"軸 '{axis}' の可動端センサ '{sensor}' が押されているため、"
            "その向きへは動かしません。**離れる向きの指令は通ります**"
        )

    def check_torque(self, *, axis: str, torque: float | None) -> None:
        """フィードバックのトルクが急に立ったら止める。

        当たったことは位置が進まないことより先にトルクへ出るので、停滞判定より
        早く効く。**測れないモータ (`None`) では何もしない** —— 常に 0 を運ぶ値で
        判定すると「測ったように見える 0」がそのまま「異常なし」に化ける。

        Raises:
            GuardViolation: `stall_torque` を超えた
        """
        limit = self._spec.stall_torque
        if limit is None or torque is None or abs(torque) <= limit:
            return
        raise GuardViolation(
            f"軸 '{axis}' のトルクが {abs(torque):.3g}Nm まで立ちました"
            f" (上限 {limit}Nm)。機構が何かに当たっているか、可動端を越えています"
        )
