"""ステップ応答から PID 調整の判断材料を数値で取り出す。

**この層は CAN も asyncio も知らない純関数だけで構成する。** 指標の定義を実機なしで
固定できることが要点で、ここが制御ループに埋まっていると「オーバーシュートの定義が
変わった」ことを誰も検出できないまま画面の数字だけが変わる。

調整で見たい量は 4 つに分かれ、それぞれ次の行動が違う:

- **飽和しているか** — 出力が上限に張り付いている間、ゲインを変えても応答は変わらない。
  これを知らずに kp を動かすと「効かない」という誤った結論に至る。最初に見る
- **速さ** (立ち上がり時間) と **行き過ぎ** (オーバーシュート) — kp と kd の綱引き
- **残るずれ** (定常偏差) — ki の要否
- **振動** (周波数と減衰) — kp 過大か、kd によるノイズ増幅か
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass

__all__ = [
    "Sample",
    "StepMetrics",
    "analyze_step_response",
    "settle_band_for",
    "step_span",
]


@dataclass(frozen=True)
class Sample:
    """制御周期 1 回分の記録。

    ``t`` はステップが入った瞬間を 0 とした相対秒で、**負の値はステップ直前**を指す。
    絶対時刻ではなく相対秒にしてあるのは、複数の応答を同じ横軸で重ねて比較するのが
    調整作業そのものだからで、比較のたびに引き算をやり直させない。
    """

    t: float
    target: float
    position: float
    #: PID 出力 (C620 電流指令 [counts])。実測電流ではなく指令値
    output: float
    #: この周期の出力が出力レンジの端に張り付いたか
    saturated: bool


@dataclass(frozen=True)
class StepMetrics:
    """1 回のステップ応答から読み取れた指標。

    測れなかった項目は ``None`` にする。0 で埋めると「行き過ぎが無かった」と
    「窓の中で目標に届かなかった」が同じ表示になり、次に取るべき行動が正反対に
    なる (前者は kp を上げてよい、後者は上げ方の問題ではない)。
    """

    step_from: float
    step_to: float
    step_size: float
    #: 10% → 90% 到達時間 [s]。窓内に 90% へ届かなければ None
    rise_time_s: float | None
    #: 行き過ぎ量 [%]。届かなかった場合でも 0 以上の実測値を返す
    overshoot_pct: float
    #: 最大行き過ぎの時刻 [s]
    peak_time_s: float | None
    #: 整定時間 [s]。窓の終端まで整定帯へ収まらなければ None
    settling_time_s: float | None
    #: 窓の終盤に残った偏差 (目標 - 実測)。符号付き
    steady_state_error: float
    #: 振動周波数 [Hz]。整定帯を超える極値が 2 つ未満なら None
    oscillation_hz: float | None
    #: 対数減衰率から求めた減衰比。1 に近いほど振動しない
    damping_ratio: float | None
    #: 出力が飽和していた周期の割合 (0.0〜1.0)
    saturation_ratio: float
    #: 最大出力の絶対値 [counts]
    peak_output: float
    #: 整定判定に使った帯幅 (片側)
    settle_band: float
    sample_count: int
    duration_s: float


def step_span(
    samples: list[Sample] | tuple[Sample, ...],
) -> tuple[float, float] | None:
    """ステップの始点 (実測位置) と終点 (目標) を返す。ステップが無ければ None。

    整定帯はステップ幅から決まり、その整定帯は解析の入力でもある。順序が
    「幅を測る → 帯を決める → 解析する」になるので、幅だけを先に取れる口を分けてある。
    """
    post = [s for s in samples if s.t >= 0.0]
    if len(post) < 2:
        return None
    return post[0].position, post[-1].target


def settle_band_for(step_size: float, *, ratio: float, minimum: float) -> float:
    """整定判定の帯幅 (片側) を決める。

    ステップ幅に対する比で取るのが制御工学の慣習だが、**下限を必ず置く**。
    PID の不感帯 (``dead_band``) の内側では偏差が 0 として扱われて制御自体が
    働かないため、それより狭い帯で「整定していない」と判定すると、機構が正常でも
    永久に整定しない応答として記録され続ける。``minimum`` には不感帯を渡す。
    """
    return max(minimum, abs(step_size) * ratio)


def analyze_step_response(
    samples: list[Sample] | tuple[Sample, ...],
    *,
    settle_band: float,
) -> StepMetrics | None:
    """ステップ応答を指標へ落とす。指標を出せない入力では None を返す。

    None になるのは「ステップと呼べる入力が無かった」場合だけで、応答が悪いことは
    None の理由にしない。悪い応答こそ数値で見せる必要がある。
    """
    # ステップ以前の記録は波形として見せる価値があるが、指標には入れない
    # (立ち上がり時間の起点が押した瞬間より前にずれる)
    post = [s for s in samples if s.t >= 0.0]
    if len(post) < 2:
        return None

    step_from = post[0].position
    step_to = post[-1].target
    step_size = step_to - step_from
    if abs(step_size) < 1e-9:
        # 目標が動いていない (あるいは既に到達済み)。立ち上がりも行き過ぎも
        # 定義できないので、指標を捏造せず「解析対象ではない」と答える
        return None

    duration = post[-1].t - post[0].t

    # 正負どちらのステップでも 0 → 1 へ進む正規化応答に直す。符号ごとに分岐を
    # 書くと、片方の向きだけ条件を書き間違えても半分のケースでは正しく動く
    progress = [(s.t, (s.position - step_from) / step_size) for s in post]

    rise = _rise_time(progress)
    peak_time, peak_value = _peak(progress)
    overshoot = max(0.0, (peak_value - 1.0)) * 100.0

    errors = [(s.t, step_to - s.position) for s in post]
    settling = _settling_time(errors, settle_band)
    steady = _steady_state_error(errors)
    hz, damping = _oscillation(errors, settle_band)

    saturated = sum(1 for s in post if s.saturated)
    peak_output = max(abs(s.output) for s in post)

    return StepMetrics(
        step_from=step_from,
        step_to=step_to,
        step_size=step_size,
        rise_time_s=rise,
        overshoot_pct=overshoot,
        peak_time_s=peak_time,
        settling_time_s=settling,
        steady_state_error=steady,
        oscillation_hz=hz,
        damping_ratio=damping,
        saturation_ratio=saturated / len(post),
        peak_output=peak_output,
        settle_band=settle_band,
        sample_count=len(post),
        duration_s=duration,
    )


def _crossing(progress: list[tuple[float, float]], level: float) -> float | None:
    """正規化応答が ``level`` を最初に超えた時刻。サンプル間は線形補間する。

    補間するのは、制御周期 200Hz に対して立ち上がりが数十 ms のとき、サンプル
    グリッドへ丸めると 5ms 刻みの階段になって kp の違いが見えなくなるため。
    """
    prev_t, prev_y = progress[0]
    if prev_y >= level:
        return prev_t
    for t, y in progress[1:]:
        if y >= level:
            span = y - prev_y
            if span <= 0:
                return t
            return prev_t + (t - prev_t) * (level - prev_y) / span
        prev_t, prev_y = t, y
    return None


def _rise_time(progress: list[tuple[float, float]]) -> float | None:
    lo = _crossing(progress, 0.1)
    hi = _crossing(progress, 0.9)
    if lo is None or hi is None:
        return None
    return max(0.0, hi - lo)


def _peak(progress: list[tuple[float, float]]) -> tuple[float | None, float]:
    peak_t, peak_y = progress[0]
    for t, y in progress:
        if y > peak_y:
            peak_t, peak_y = t, y
    return peak_t, peak_y


def _settling_time(errors: list[tuple[float, float]], band: float) -> float | None:
    """整定帯へ入って**以降ずっと**留まった最初の時刻。

    「最初に帯へ入った時刻」で答えてはならない。行き過ぎて戻る途中で帯を
    横切るだけの応答が「即座に整定した」と報告され、振動している機体ほど
    良い数字が出る。終端から遡って最後の逸脱を探す。
    """
    last_violation = None
    for index, (_, err) in enumerate(errors):
        if abs(err) > band:
            last_violation = index
    if last_violation is None:
        return errors[0][0]
    if last_violation + 1 >= len(errors):
        # 窓の終端まで帯の外。整定時間は「まだ分からない」であって 0 でも窓長でもない
        return None
    return errors[last_violation + 1][0]


def _steady_state_error(errors: list[tuple[float, float]]) -> float:
    """窓の終盤 20% の平均偏差。

    最終サンプル 1 点で代表させると、ノイズ 1 点や振動の位相で符号ごと変わる。
    ki を入れるかどうかの判断材料なので、向きが反転しては使えない。
    """
    tail_start = max(0, len(errors) - max(1, len(errors) // 5))
    tail = errors[tail_start:]
    return sum(err for _, err in tail) / len(tail)


def _oscillation(
    errors: list[tuple[float, float]], band: float
) -> tuple[float | None, float | None]:
    """偏差波形の極値から振動周波数と減衰比を推定する。

    整定帯を超える極値だけを数える。帯の内側の揺れは機構にとって無害で、
    そこまで拾うと「静止しているのに 40Hz で振動している」という、フィードバック
    量子化ノイズを読んだだけの数字が出る。
    """
    extrema = _extrema(errors, band)
    if len(extrema) < 2:
        return None, None

    # 隣り合う極値は半周期ぶん離れている
    intervals = [b[0] - a[0] for a, b in itertools.pairwise(extrema)]
    half_period = sum(intervals) / len(intervals)
    hz = 1.0 / (2.0 * half_period) if half_period > 0 else None

    damping = None
    ratios = [
        abs(a[1]) / abs(b[1])
        for a, b in itertools.pairwise(extrema)
        if abs(b[1]) > 1e-12 and abs(a[1]) > abs(b[1])
    ]
    if ratios:
        # 対数減衰率。隣り合う極値は半周期ぶんなので 2 倍して 1 周期あたりに直す
        delta = 2.0 * (sum(math.log(r) for r in ratios) / len(ratios))
        damping = delta / math.sqrt(4.0 * math.pi**2 + delta**2)
    return hz, damping


def _extrema(errors: list[tuple[float, float]], band: float) -> list[tuple[float, float]]:
    """偏差の局所極値のうち、整定帯を超える振幅を持つものだけを時刻順に返す。"""
    found: list[tuple[float, float]] = []
    for index in range(1, len(errors) - 1):
        t, err = errors[index]
        if abs(err) <= band:
            continue
        prev_err = errors[index - 1][1]
        next_err = errors[index + 1][1]
        is_max = err >= prev_err and err > next_err
        is_min = err <= prev_err and err < next_err
        if is_max or is_min:
            found.append((t, err))
    return found
