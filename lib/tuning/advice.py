"""指標から「次にどのゲインをどちらへ動かすか」を言葉にする。

助言は指標の言い換えに過ぎず、**新しい判定を持ち込まない**。判断材料そのものは
``metrics.py`` が持ち、ここは同じ数値へ名前を付けるだけにする。ここで独自の
しきい値判定を足すと、画面の波形・指標と助言が食い違う状態が作れてしまい、
操縦者はどちらを信じればよいか分からなくなる。

**順序に意味がある。** 飽和を最初に見るのは、出力が上限に張り付いている間は
ゲインを動かしても応答が変わらないため。これを後回しにすると「kp を上げても
下げても同じ」という観察から、操縦者は制御以外の原因 (機構の噛み込み・
``output_limit`` の設定・ステップ幅の取りすぎ) へ辿り着けない。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from lib.tuning.metrics import StepMetrics

__all__ = ["DEFAULT_ADVICE", "Advice", "AdviceSeverity", "AdviceThresholds", "advise"]


class AdviceSeverity(StrEnum):
    """助言の重み。UI の色分けはこれだけで決める。"""

    #: 調整の余地が見当たらない
    OK = "ok"
    #: 次に試せることがある (悪い状態とは限らない)
    INFO = "info"
    #: このまま運用すると機構か制御に無理が出る
    WARNING = "warning"


@dataclass(frozen=True)
class AdviceThresholds:
    """助言を出す境界。

    **これは機体ごとの設定ではなく助言規則そのものの定義なので config に出さない。**
    config へ逃がすと、操縦者が機体ではなく助言器のほうを調整でき、しかも
    「助言が出ないから正常」という読み方が成立してしまう。1 組の値として持つのは、
    一部だけ差し替えた状態を構文的に作れないようにするため。
    """

    #: この割合以上の周期で出力が飽和していたら、ゲインより先に飽和を疑う
    saturation_ratio: float = 0.3
    #: 行き過ぎがこの値 [%] を超えたら制動不足とみなす
    overshoot_pct: float = 20.0
    #: 減衰比がこの値を下回る振動は「ほとんど収まっていない」
    damping_ratio: float = 0.2
    #: 立ち上がりに余裕があると判断する行き過ぎ量 [%]
    calm_overshoot_pct: float = 5.0


DEFAULT_ADVICE = AdviceThresholds()


@dataclass(frozen=True)
class Advice:
    """1 件の助言。``code`` は UI とテストが名前で参照するための安定した識別子。"""

    code: str
    severity: AdviceSeverity
    message: str


def advise(
    metrics: StepMetrics,
    *,
    kp: float,
    ki: float,
    kd: float,
    thresholds: AdviceThresholds = DEFAULT_ADVICE,
) -> list[Advice]:
    """指標とゲインから助言を組み立てる。重い順に並べて返す。"""
    found: list[Advice] = []

    if metrics.saturation_ratio >= thresholds.saturation_ratio:
        found.append(
            Advice(
                "saturated",
                AdviceSeverity.WARNING,
                f"出力が全周期の {metrics.saturation_ratio * 100:.0f}% で上限に達しています。"
                "飽和している間はゲインを変えても応答は変わりません。"
                "ステップ幅・config の output_limit・機構の負荷を先に確認してください。",
            )
        )

    if metrics.settling_time_s is None:
        found.append(
            Advice(
                "not_settled",
                AdviceSeverity.WARNING,
                f"記録した {metrics.duration_s:.1f}s のあいだ整定帯 "
                f"(±{metrics.settle_band:.2f}) へ収まりませんでした。",
            )
        )

    damping = metrics.damping_ratio
    if metrics.oscillation_hz is not None and (
        damping is None or damping < thresholds.damping_ratio
    ):
        message = f"{metrics.oscillation_hz:.1f}Hz の振動がほとんど減衰していません。"
        # kd が既に入っている状態の振動は、制動不足ではなく微分がノイズを増幅している
        # 可能性がある。同じ「振動」でも kd を足すか減らすかで正反対になる
        if kd > 0.0:
            message += (
                "kp を下げるか、kd がフィードバックのノイズを増幅していないか確認してください。"
            )
        else:
            message += "kp を下げるか、kd を少しずつ入れて制動を足してください。"
        found.append(Advice("oscillating", AdviceSeverity.WARNING, message))

    elif metrics.overshoot_pct > thresholds.overshoot_pct:
        found.append(
            Advice(
                "overshoot",
                AdviceSeverity.INFO,
                f"行き過ぎが {metrics.overshoot_pct:.0f}% あります。"
                "kd を足すか kp を下げると収まります。",
            )
        )

    residual = abs(metrics.steady_state_error)
    if residual > metrics.settle_band:
        if ki == 0.0:
            found.append(
                Advice(
                    "steady_state_error",
                    AdviceSeverity.INFO,
                    f"偏差が {metrics.steady_state_error:+.2f} 残っています。"
                    "ki を入れると詰められます。"
                    "その際 integral_limit を必ず設定してください "
                    "(機構端で積分が育つと拘束が外れた瞬間に暴走します)。",
                )
            )
        else:
            found.append(
                Advice(
                    "steady_state_error",
                    AdviceSeverity.INFO,
                    f"ki={ki} でも偏差が {metrics.steady_state_error:+.2f} 残っています。"
                    "ki を上げるか、integral_limit で頭打ちになっていないか確認してください。",
                )
            )

    if metrics.rise_time_s is None and metrics.saturation_ratio < thresholds.saturation_ratio:
        found.append(
            Advice(
                "too_slow",
                AdviceSeverity.INFO,
                "記録した窓の中で目標の 90% に届いていません。kp を上げてください。",
            )
        )
    elif (
        metrics.rise_time_s is not None
        and metrics.settling_time_s is not None
        and metrics.overshoot_pct <= thresholds.calm_overshoot_pct
        and metrics.oscillation_hz is None
    ):
        found.append(
            Advice(
                "headroom",
                AdviceSeverity.INFO,
                f"行き過ぎ {metrics.overshoot_pct:.0f}% / 立ち上がり "
                f"{metrics.rise_time_s * 1000:.0f}ms で振動もありません。"
                "もっと速くしたいなら kp を上げる余地があります。",
            )
        )

    if not found:
        found.append(Advice("ok", AdviceSeverity.OK, "指標から見て気になる点はありません。"))

    # 重い順。WARNING が INFO の下に埋もれると、飽和や未整定を読み飛ばす
    order = {AdviceSeverity.WARNING: 0, AdviceSeverity.INFO: 1, AdviceSeverity.OK: 2}
    found.sort(key=lambda a: order[a.severity])
    return found
