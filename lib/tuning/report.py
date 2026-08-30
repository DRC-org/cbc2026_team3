"""記録した窓を「波形 + 指標 + 助言」の 1 通にまとめる。

**3 つを 1 通で運ぶ。** 分けて配ると、波形だけ届いて指標が来ていない画面や、
指標が新しく波形が古い画面が作れてしまう。調整はこの 3 つを突き合わせる作業
なので、途中の 1 通を落とした画面はそのまま誤読につながる (動作確認の
``motor_check_state`` を 1 通に畳んだのと同じ理由)。

解析はこのモジュールで行い、制御ループの中では行わない。200Hz の周期処理に
サンプル数に比例する仕事を入れると、負荷に応じて制御周期が伸びる。
"""

from __future__ import annotations

from dataclasses import dataclass

from lib.tuning.advice import Advice, advise
from lib.tuning.metrics import (
    Sample,
    StepMetrics,
    analyze_step_response,
    settle_band_for,
    step_span,
)
from lib.tuning.recorder import Capture

__all__ = ["DEFAULT_BAND_RATIO", "DEFAULT_MAX_POINTS", "CaptureReport", "summarize"]

#: 整定帯をステップ幅の何割で取るか。制御工学の慣習どおり 2%
DEFAULT_BAND_RATIO = 0.02

#: 配信する波形の最大点数。200Hz で 3 秒ぶんの 600 点をそのまま送ると 1 回の記録で
#: 数十 KB になり、動作確認のように複数軸が同時に動く場面ではその倍数が
#: 一度に流れる。解析はサーバー側で全点に対して行うので、間引いてよいのは
#: 表示だけ (画面の横幅は 1000px 未満で、それ以上の点は描いても見えない)
DEFAULT_MAX_POINTS = 300


@dataclass(frozen=True)
class CaptureReport:
    """1 回のステップ応答の解析結果。"""

    robot: str
    capture: Capture
    #: ステップとして解釈できなかった記録では None (助言も空になる)
    metrics: StepMetrics | None
    advice: list[Advice]

    def to_payload(self, *, max_points: int = DEFAULT_MAX_POINTS) -> dict:
        """WS へ載せる 1 通を組み立てる。

        波形は列ごとの配列で運ぶ。サンプルごとにオブジェクトを作ると、同じキー名が
        点数ぶん繰り返されて JSON が 3 倍近くなる。

        **ゲインは丸めない。** 表示された値と機体の実際のゲインが食い違うと、
        操縦者は自分が送った値を画面から確認できなくなる (/pid-tuning が
        `toFixed` を使わないのと同じ理由)。波形の実測値はセンサ分解能より
        細かい桁に意味が無いので丸めて送る。
        """
        samples = _decimate(self.capture.samples, max_points)
        gains = self.capture.gains
        return {
            "type": "tuning_capture",
            "robot": self.robot,
            "motor": self.capture.motor,
            "captured_at": self.capture.captured_at,
            "gains": {"kp": gains.kp, "ki": gains.ki, "kd": gains.kd},
            "metrics": _metrics_payload(self.metrics),
            "advice": [
                {"code": a.code, "severity": str(a.severity), "message": a.message}
                for a in self.advice
            ],
            "samples": {
                "t": [round(s.t, 4) for s in samples],
                "target": [round(s.target, 4) for s in samples],
                "pos": [round(s.position, 4) for s in samples],
                "output": [round(s.output, 1) for s in samples],
                "sat": [s.saturated for s in samples],
            },
        }


def summarize(
    robot: str,
    capture: Capture,
    *,
    band_ratio: float = DEFAULT_BAND_RATIO,
) -> CaptureReport:
    """記録を解析して配信できる形にする。"""
    span = step_span(capture.samples)
    if span is None:
        return CaptureReport(robot=robot, capture=capture, metrics=None, advice=[])

    step_from, step_to = span
    band = settle_band_for(
        step_to - step_from,
        ratio=band_ratio,
        # 不感帯の内側は偏差 0 として扱われ制御が働かない。そこより狭い帯で
        # 整定を判定すると、機構が正常でも永久に整定しない応答になる
        minimum=capture.gains.dead_band,
    )
    metrics = analyze_step_response(capture.samples, settle_band=band)
    if metrics is None:
        return CaptureReport(robot=robot, capture=capture, metrics=None, advice=[])

    return CaptureReport(
        robot=robot,
        capture=capture,
        metrics=metrics,
        advice=advise(
            metrics,
            kp=capture.gains.kp,
            ki=capture.gains.ki,
            kd=capture.gains.kd,
        ),
    )


def _metrics_payload(metrics: StepMetrics | None) -> dict | None:
    if metrics is None:
        return None
    return {
        "step_from": round(metrics.step_from, 4),
        "step_to": round(metrics.step_to, 4),
        "step_size": round(metrics.step_size, 4),
        "rise_time_s": _round_opt(metrics.rise_time_s, 4),
        "overshoot_pct": round(metrics.overshoot_pct, 2),
        "peak_time_s": _round_opt(metrics.peak_time_s, 4),
        "settling_time_s": _round_opt(metrics.settling_time_s, 4),
        "steady_state_error": round(metrics.steady_state_error, 4),
        "oscillation_hz": _round_opt(metrics.oscillation_hz, 3),
        "damping_ratio": _round_opt(metrics.damping_ratio, 4),
        "saturation_ratio": round(metrics.saturation_ratio, 4),
        "peak_output": round(metrics.peak_output, 1),
        "settle_band": round(metrics.settle_band, 4),
        "sample_count": metrics.sample_count,
        "duration_s": round(metrics.duration_s, 4),
    }


def _round_opt(value: float | None, digits: int) -> float | None:
    return None if value is None else round(value, digits)


def _decimate(samples: tuple[Sample, ...], max_points: int) -> list[Sample]:
    """表示用に点数を落とす。**各区間から最も目標を外れた点を残す。**

    等間隔に拾うだけの間引きは、行き過ぎのピークがちょうど落とされた区間に
    あると波形からオーバーシュートが消える。指標は「行き過ぎ 30%」と言うのに
    グラフは行き過ぎていない、という食い違いが起きるため、区間の代表は
    偏差が最大の点にする。
    """
    if max_points <= 0 or len(samples) <= max_points:
        return list(samples)

    buckets: list[Sample] = []
    total = len(samples)
    for index in range(max_points):
        start = index * total // max_points
        end = max(start + 1, (index + 1) * total // max_points)
        chunk = samples[start:end]
        buckets.append(max(chunk, key=lambda s: abs(s.target - s.position)))

    # 端は必ず残す。窓の始点と終点が欠けると、立ち上がりの起点と定常値が読めない
    if buckets[0] is not samples[0]:
        buckets[0] = samples[0]
    if buckets[-1] is not samples[-1]:
        buckets[-1] = samples[-1]
    return buckets
