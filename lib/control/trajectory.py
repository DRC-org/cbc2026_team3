"""速度・加速度を制限した中間目標を生成する台形速度プロファイル。

``M3508PositionLoop`` は ``move_to`` の最終目標をステップで PID に入れている。
``y_axis`` の実測値 (``scale`` 55.0131 deg/mm, ``kp`` 32 counts/deg,
``output_limit`` 2000 counts) では偏差 1.14mm で P 項が飽和するため、飽和中の PID は
制御ではなくフル電流の定加速になる。減速に使える距離は「偏差が 1.14mm を切ってから」で
**移動距離に依らず一定**なのに、そこまでに乗る速度は移動距離とともに増えるので、
止まりきれる移動距離は約 2.3mm が上限になる。実機で検証済みの 1.5mm はその内側、
実運用ストローク 5〜15mm は外側で、原理的に行き過ぎる。``kd`` を上げても直らない
(飽和中は合計がクランプされ D 項も出力に現れない)。

そこで PID には最終目標ではなく「速度・加速度で制限した中間目標」を毎周期与え、
追従誤差だけを見せる。速度が初めて明示的なつまみになる (従来は従属変数だった)。

このモジュールは最下位層で、CAN も asyncio も ``lib.control`` の他モジュールも
import しない (``lib/axis_sync.py`` と同じ扱い)。単位は呼び出し側が決める ——
位置制御ループはモータの指令単位 (deg) で使うが、この実装はどの単位でも成立する。

**時刻ではなく ``dt`` で進める。** asyncio のジッタで周期が揺れるため固定 dt を
前提にできず、移動中の再ターゲット (手動ジョグの連打) も「今の速度から作り直す」形で
自然に扱える。

**減速の判定は連続時間の停止距離 ``v^2/(2a)`` では足りない。** その式で分岐すると
1 周期ぶんの取りこぼしが積もり、実測で 200Hz / 60mm/s / 400mm/s^2 のとき最大 0.29mm
行き過ぎる (``y_axis`` の ``sync_tolerance`` 2.0mm の 15%)。ここでは代わりに
「この周期に出してよい速度の上限」を離散時間の停止距離から**厳密に**解いて、
毎周期その上限へ向けて加速度制限つきで寄せる。行き過ぎないことが分岐の書き方ではなく
上限の定義そのものから出るので、周期や距離を変えても崩れない。
"""

from __future__ import annotations

import math

__all__ = ["TrapezoidalProfile"]


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _stoppable_velocity(distance: float, acceleration: float, dt: float) -> float:
    """残り ``distance`` を跨がずに止まりきれる、この周期の速度の上限。

    速度を先に更新してから ``位置 += 速度 * dt`` で積む順序なので、速度 ``v`` を
    出した時点で確定する残りの移動量は ``dt * Σ_{k=0..m} (v - k*a*dt)``
    (``m`` は 0 になるまでの段数) である。これが ``distance`` 以下になる最大の ``v`` を
    返す。``m`` の区間ごとに線形なので、区間を決めてから 1 次方程式を解けば厳密に出る。

    連続時間の ``sqrt(2*a*distance)`` を使わないのは、それが「速度 v から止まるまでの
    距離」であって「今の周期に進む分」を含まないため。含めないと 1 周期ぶん行き過ぎる。
    残距離が ``a*dt^2`` 未満のときは ``m=0`` になり、答えが ``distance/dt`` ——
    ちょうど目標へ着地する速度 —— に退化するので、終端の 1 周期も同じ式で決まる。
    """
    if distance <= 0.0:
        return 0.0

    # m*(m+1) <= quota が「m 段で止まりきれる」条件
    quota = 2.0 * distance / (acceleration * dt * dt)
    steps = math.floor((math.sqrt(1.0 + 4.0 * quota) - 1.0) / 2.0)
    # sqrt の丸めで段数が 1 つずれると上限が壊れるので、条件式そのもので確かめ直す
    while steps > 0 and steps * (steps + 1) > quota:
        steps -= 1
    while (steps + 1) * (steps + 2) <= quota:
        steps += 1

    return (distance / dt + acceleration * dt * steps * (steps + 1) / 2.0) / (steps + 1)


class TrapezoidalProfile:
    """目標値を速度・加速度で制限しながら追いかける中間目標の生成器。

    位置と速度の内部状態を持ち、``advance`` を制御周期ごとに 1 回呼ぶ。返す位置を
    PID の setpoint に、速度を速度フィードフォワードに使う。
    """

    def __init__(self, *, max_velocity: float, max_acceleration: float) -> None:
        """
        Args:
            max_velocity: 速度の上限 (正)。単位は位置と揃える
            max_acceleration: 加速度の上限 (正)。減速側にも同じ値が掛かる

        Raises:
            ValueError: 上限が 0 以下のとき。0 を許すと軌道が一切進まない
                「必ずタイムアウトする軸」が config から作れてしまう
        """
        if max_velocity <= 0.0:
            raise ValueError(f"max_velocity は正の値: {max_velocity}")
        if max_acceleration <= 0.0:
            raise ValueError(f"max_acceleration は正の値: {max_acceleration}")

        self._max_velocity = max_velocity
        self._max_acceleration = max_acceleration
        self._position = 0.0
        self._velocity = 0.0
        self._target = 0.0

    @property
    def position(self) -> float:
        """現在の中間目標。"""
        return self._position

    @property
    def velocity(self) -> float:
        """現在の参照速度。"""
        return self._velocity

    @property
    def done(self) -> bool:
        """最終目標へ到達して静止しているか。"""
        return self._position == self._target and self._velocity == 0.0

    def reset(self, position: float) -> None:
        """指定位置へ張り付け、速度を 0 にする。

        最終目標も同じ位置へ落とす。残しておくと、リセットした次の 1 周期で古い目標へ
        向かって動き出す —— 緊急停止やフィードバック途絶からの復帰でこれが起きると、
        止まっていた間に進んだはずの中間目標へ機構が飛ぶ。
        """
        self._position = position
        self._velocity = 0.0
        self._target = position

    def retarget(self, target: float) -> None:
        """最終目標だけを差し替える。速度は保つ。

        移動中の差し替え (手動ジョグの連打・シーケンスの次ステップ) で速度を捨てると、
        そこで一度止まってから動き直すことになり、加速度制限を割った段差が出る。
        """
        self._target = target

    def advance(self, dt: float) -> tuple[float, float]:
        """1 周期分進めて ``(中間目標, 参照速度)`` を返す。

        Args:
            dt: 前回 ``advance`` からの経過時間 [s]

        Returns:
            進めた後の位置と速度。``dt <= 0`` では内部状態を一切更新せず現在値を
            そのまま返す (``PIDController.update`` と同じ約束。フィードバックが 2 回
            同一タイムスタンプで来ても軌道が壊れないため)。
        """
        if dt <= 0:
            return self._position, self._velocity

        remaining = self._target - self._position
        limit = _stoppable_velocity(abs(remaining), self._max_acceleration, dt)
        if remaining == 0.0:
            desired = 0.0
        else:
            desired = math.copysign(min(self._max_velocity, limit), remaining)

        # 加速度制限は目標速度の決め方に依らず最後に一律で掛ける。逆向きへの再ターゲットも
        # ここを通るので、向きが変わる周期だけ段差が出ることがない
        step_limit = self._max_acceleration * dt
        velocity = _clamp(desired, self._velocity - step_limit, self._velocity + step_limit)

        step = velocity * dt
        if remaining != 0.0 and step * remaining > 0.0 and abs(step) >= abs(remaining):
            # 着地の周期。加算の丸め誤差で目標を跨いだり手前で止まったりしないよう代入する
            self._position = self._target
        else:
            self._position += step
        self._velocity = velocity
        return self._position, self._velocity
