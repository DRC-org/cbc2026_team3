import { cx } from "@/lib/cx";
import type { Tone } from "@/lib/tone";
import { TONE_PROGRESS_CLASS, TONE_TEXT_CLASS } from "@/lib/tone";

interface SequenceProgressProps {
  sequence: string;
  currentStep: string | null;
  stepIndex: number;
  totalSteps: number;
  waitingTrigger: boolean;
}

type StatusKey = "complete" | "waiting" | "running" | "idle";

// 記号 + セマンティック色でステータスを表現する。
const STATUS: Record<StatusKey, { label: string; symbol: string; tone: Tone }> = {
  complete: { label: "Done", symbol: "✓", tone: "success" },
  waiting: { label: "Awaiting approval", symbol: "▮", tone: "warning" },
  running: { label: "Running", symbol: "►", tone: "info" },
  idle: { label: "Not started", symbol: "○", tone: "neutral" },
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
    <section className="flex flex-col gap-1">
      <div className="hsplit">
        <div className="hstack items-baseline">
          <span className="text-fg-dim">SEQ</span>
          <span className="truncate">{sequence}</span>
        </div>
        <span className={cx("whitespace-nowrap", TONE_TEXT_CLASS[status.tone])}>
          [{status.symbol} {status.label}]
        </span>
      </div>

      <div className="hsplit">
        <div className="hstack items-baseline">
          <span className="tabular-nums">{displayIndex}</span>
          <span className="text-fg-dim">/ {totalSteps}</span>
          <span className="truncate" title={currentStep ?? undefined}>
            {currentStep ? `› ${currentStep}` : "—"}
          </span>
        </div>
        <span className="whitespace-nowrap tabular-nums">{Math.round(percent)}%</span>
      </div>

      <progress
        className={cx(
          "progress h-[0.9rem] w-full border border-line bg-base-300",
          TONE_PROGRESS_CLASS[status.tone],
        )}
        value={percent}
        max={100}
      />
    </section>
  );
}
