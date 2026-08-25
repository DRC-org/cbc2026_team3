from __future__ import annotations

import math

__all__ = ["PIDController"]


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


class PIDController:
    """モータ非依存の PID コントローラ。

    CAN や asyncio に依存しない同期クラス。時刻取得も内部で行わず、制御周期 ``dt`` を
    毎回引数で受け取る (asyncio ループのジッタで周期が揺れるため固定 dt を前提にできない)。

    M3508 は C620 ESC 経由で電流指令しか受け付けないため、位置制御はこのクラスで
    ``位置 [deg] → 電流指令`` に変換して使う。その場合は
    ``output_min=-16384`` / ``output_max=16384`` (lib/drivers/m3508.py の
    ``CURRENT_MIN`` / ``CURRENT_MAX``) を指定する。

    ゲイン ``kp`` / ``ki`` / ``kd`` は実行中のチューニング UI から書き換える想定で
    公開属性にしている。
    """

    def __init__(
        self,
        kp: float,
        ki: float = 0.0,
        kd: float = 0.0,
        *,
        output_min: float = -math.inf,
        output_max: float = math.inf,
        integral_limit: float | None = None,
        dead_band: float = 0.0,
    ) -> None:
        """
        Args:
            kp: 比例ゲイン
            ki: 積分ゲイン (0 のとき積分は蓄積しない)
            kd: 微分ゲイン (測定値微分に掛かる)
            output_min: 出力下限
            output_max: 出力上限
            integral_limit: 積分項の出力寄与 ``|ki * integral|`` の上限 (出力と同じ単位)
            dead_band: 偏差の不感帯。``|error| <= dead_band`` では P/I を進めない
        """
        if output_min > output_max:
            raise ValueError(f"output_min は output_max 以下: {output_min} > {output_max}")
        if integral_limit is not None and integral_limit < 0:
            raise ValueError(f"integral_limit は 0 以上: {integral_limit}")
        if dead_band < 0:
            raise ValueError(f"dead_band は 0 以上: {dead_band}")

        self.kp = kp
        self.ki = ki
        self.kd = kd
        self.output_min = output_min
        self.output_max = output_max
        self.integral_limit = integral_limit
        self.dead_band = dead_band

        self._integral = 0.0
        self._prev_measurement: float | None = None
        self._last_output = 0.0

    @property
    def integral(self) -> float:
        """積分器の内部状態 (``ki`` を掛ける前の値)。"""
        return self._integral

    @property
    def last_output(self) -> float:
        return self._last_output

    def reset(self) -> None:
        """積分項と前回測定値をクリアする。シーケンス再開時・非常停止解除時に呼ぶ。"""
        self._integral = 0.0
        self._prev_measurement = None
        self._last_output = 0.0

    def update(self, setpoint: float, measurement: float, dt: float) -> float:
        """偏差を 1 周期分処理して操作量を返す。

        Args:
            setpoint: 目標値
            measurement: 現在値 (モータフィードバック)
            dt: 前回 update からの経過時間 [s]

        Returns:
            ``output_min``〜``output_max`` にクランプされた操作量。
            ``dt <= 0`` の場合は内部状態を一切更新せず前回出力をそのまま返す
            (フィードバックが 2 回同一タイムスタンプで来ても微分がゼロ除算で発散しないため)。
        """
        if dt <= 0:
            return self._last_output

        error = setpoint - measurement
        # 目標付近の微小電流でモータが唸るのを防ぐ。ただし積分項の蓄積分は出力に残すので
        # 昇降軸の保持力 (重力補償) は失われない
        if abs(error) <= self.dead_band:
            error = 0.0

        proportional = self.kp * error

        # 目標値ステップで D 項がスパイクし機構に衝撃を与えないよう、偏差ではなく測定値を微分する
        derivative = 0.0
        if self._prev_measurement is not None:
            derivative = -self.kd * (measurement - self._prev_measurement) / dt

        # ki=0 のまま積分を回すと、後からゲインを上げた瞬間に蓄積分が一気に出力へ乗る
        candidate_integral = self._integral
        if self.ki != 0.0:
            candidate_integral += error * dt
            if self.integral_limit is not None:
                bound = self.integral_limit / abs(self.ki)
                candidate_integral = _clamp(candidate_integral, -bound, bound)

        unclamped = proportional + self.ki * candidate_integral + derivative
        output = _clamp(unclamped, self.output_min, self.output_max)

        # conditional integration: 出力が飽和していて、かつ今回の積分が飽和を深める向きなら
        # 積分を進めない。機構端に当たって動けない間に積分が育つと、拘束が外れた瞬間に暴走する
        if unclamped != output and error * (unclamped - output) > 0:
            unclamped = proportional + self.ki * self._integral + derivative
            output = _clamp(unclamped, self.output_min, self.output_max)
        else:
            self._integral = candidate_integral

        self._prev_measurement = measurement
        self._last_output = output
        return output
