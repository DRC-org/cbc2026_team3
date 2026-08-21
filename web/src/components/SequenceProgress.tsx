import { ProgressBar } from "@tsaito18/tuicss-react";

import type { TuiColor } from "@/lib/tuiColor";

interface SequenceProgressProps {
  sequence: string;
  currentStep: string | null;
  stepIndex: number;
  totalSteps: number;
  waitingTrigger: boolean;
}

type StatusKey = "complete" | "waiting" | "running" | "idle";

// TUI 記号 + セマンティック色でステータスを表現する。
const STATUS: Record<StatusKey, { label: string; symbol: string; color: TuiColor }> = {
  complete: { label: "Done", symbol: "✓", color: "success" },
  waiting: { label: "Awaiting approval", symbol: "▮", color: "warning" },
  running: { label: "Running", symbol: "►", color: "info" },
  idle: { label: "Not started", symbol: "○", color: "secondary" },
};

const PROGRESS_TONE_CLASS: Record<TuiColor, string> = {
  success: "progress-ok",
  warning: "progress-warn",
  danger: "progress-danger",
  info: "progress-info",
  secondary: "",
};

export function SequenceProgress({
  sequence,
  currentStep,
  stepIndex,
  totalSteps,
  waitingTrigger,
}: SequenceProgressProps) {
  // バックエンドは完走時に step_index = total_steps を返すため、
  // 表示用には total を超えないようクランプし、% も 0..100 に収める
  const isComplete = totalSteps > 0 && stepIndex >= totalSteps && !waitingTrigger;
  const displayIndex = totalSteps > 0 ? Math.min(stepIndex + 1, totalSteps) : 0;
  const percent =
    totalSteps > 0
      ? Math.min(100, ((isComplete ? totalSteps : stepIndex + 1) / totalSteps) * 100)
      : 0;
  const statusKey: StatusKey =
    totalSteps === 0 ? "idle" : isComplete ? "complete" : waitingTrigger ? "waiting" : "running";
  const status = STATUS[statusKey];

  return (
    <section style={{ display: "flex", flexDirection: "column", gap: "0.25rem" }}>
      <div className="hsplit">
        <div className="hstack" style={{ alignItems: "baseline" }}>
          <span className="dim">SEQ</span>
          <span className="ellipsis">{sequence}</span>
        </div>
        <span className={`${status.color}-text nowrap`}>
          [{status.symbol} {status.label}]
        </span>
      </div>

      <div className="hsplit">
        <div className="hstack" style={{ alignItems: "baseline" }}>
          <span className="tabular-nums">{displayIndex}</span>
          <span className="dim">/ {totalSteps}</span>
          <span className="ellipsis" title={currentStep ?? undefined}>
            {currentStep ? `› ${currentStep}` : "—"}
          </span>
        </div>
        <span className="nowrap tabular-nums">{Math.round(percent)}%</span>
      </div>

      <ProgressBar
        className={PROGRESS_TONE_CLASS[status.color]}
        trackBackground={false}
        style={{ width: "100%" }}
        value={percent}
      />
    </section>
  );
}
