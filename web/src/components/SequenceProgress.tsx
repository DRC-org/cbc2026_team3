import { ProgressBar } from "@tsaito18/tuicss-react";

import { cx } from "@/lib/cx";
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

  const progressColorClass: Record<TuiColor, string> = {
    success: "green-255",
    warning: "yellow-255",
    danger: "red-255",
    info: "cyan-255",
    secondary: "blue-255",
  };

  return (
    <section style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: 12,
        }}
      >
        <div
          style={{
            display: "flex",
            minWidth: 0,
            alignItems: "baseline",
            gap: 8,
          }}
        >
          <span style={{ opacity: 0.7 }}>SEQ</span>
          <span
            style={{
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}
          >
            {sequence}
          </span>
        </div>
        <span
          className={cx(`${status.color}-text`)}
          style={{ flexShrink: 0, whiteSpace: "nowrap" }}
        >
          [{status.symbol} {status.label}]
        </span>
      </div>

      <div
        style={{
          display: "flex",
          alignItems: "flex-end",
          justifyContent: "space-between",
          gap: 12,
        }}
      >
        <div
          style={{
            display: "flex",
            minWidth: 0,
            alignItems: "baseline",
            gap: 8,
          }}
        >
          <span className="tabular-nums">{displayIndex}</span>
          <span style={{ opacity: 0.7 }}>/ {totalSteps}</span>
          <span
            style={{
              marginLeft: 4,
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}
            title={currentStep ?? undefined}
          >
            {currentStep ? `› ${currentStep}` : "—"}
          </span>
        </div>
        <span className="tabular-nums" style={{ flexShrink: 0, opacity: 0.9 }}>
          {Math.round(percent)}%
        </span>
      </div>

      <ProgressBar
        className="tui-bg-cyan-black"
        progressClassName={progressColorClass[status.color]}
        style={{ width: "100%" }}
        value={percent}
      />
    </section>
  );
}
