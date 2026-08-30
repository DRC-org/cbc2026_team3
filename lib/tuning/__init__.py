"""PID 調整の支援層。ステップ応答の記録・指標算出・助言を持つ。

**このパッケージは機体を動かさない。** 駆動経路は既存の手動操縦とシーケンスだけで、
ここは「動いた結果を測って数値にする」ことしかしない。調整のために専用の駆動経路を
足すと機体が動く条件が 1 つ増え、フェーズゲート・緊急停止・動作確認との排他を
そのぶん多く守り続けることになる。

層は下から ``metrics`` (純関数の指標) → ``advice`` (指標の言い換え) →
``recorder`` (制御ループが呼ぶ記録器) → ``report`` (配信 1 通への組み立て)。
上位を import してよいのは上の層だけで、``metrics`` は誰も import しない。
"""

from lib.tuning.advice import Advice, AdviceSeverity, AdviceThresholds, advise
from lib.tuning.metrics import (
    Sample,
    StepMetrics,
    analyze_step_response,
    settle_band_for,
    step_span,
)
from lib.tuning.recorder import Capture, MotorStepRecorder, PidSnapshot
from lib.tuning.report import CaptureReport, summarize

__all__ = [
    "Advice",
    "AdviceSeverity",
    "AdviceThresholds",
    "Capture",
    "CaptureReport",
    "MotorStepRecorder",
    "PidSnapshot",
    "Sample",
    "StepMetrics",
    "advise",
    "analyze_step_response",
    "settle_band_for",
    "step_span",
    "summarize",
]
