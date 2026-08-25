import { Panel } from "@/components/ui/Panel";
import type { SequenceStepInfo } from "@/hooks/useRobotSocket";

interface CurrentStepPanelProps {
  steps: SequenceStepInfo[];
  stepIndex: number;
  totalSteps: number;
  waitingTrigger: boolean;
}

/**
 * 試合中の主表示。現在ステップと次ステップを大きく出す。
 *
 * 操縦者は機体を見ながら操作するため、画面に視線を戻す時間は一瞬しかない。
 * 小さな STEP LIST を目で追わずに「今どこか」「次に何が起きるか」を読めるよう、
 * この 2 つだけを大きな文字で切り出す。次ステップの ✋ (許可待ち有無) を出すのは、
 * NEXT を押した後に機体が止まるのか動き続けるのかを事前に把握させるため。
 */
export function CurrentStepPanel({
  steps,
  stepIndex,
  totalSteps,
  waitingTrigger,
}: CurrentStepPanelProps) {
  const isComplete = totalSteps > 0 && stepIndex >= totalSteps;
  const current = isComplete ? null : steps[stepIndex];
  const next = isComplete ? null : steps[stepIndex + 1];

  const status = isComplete
    ? { label: "シーケンス完走", className: "text-success", symbol: "✓" }
    : waitingTrigger
      ? { label: "許可待ち — NEXT を押してください", className: "text-warning", symbol: "▮" }
      : { label: "実行中", className: "text-info", symbol: "►" };

  return (
    <Panel legend="CURRENT STEP" className="flex-1">
      <div className="flex min-h-0 flex-1 flex-col justify-center gap-2">
        <div className={status.className}>
          [{status.symbol} {status.label}]
        </div>
        <div className="flex min-w-0 items-baseline gap-3">
          <span className="shrink-0 text-[1.6em] text-fg-dim tabular-nums">
            #{isComplete ? totalSteps : stepIndex + 1}
          </span>
          <span className="min-w-0 text-[1.6em] leading-[1.25]">
            {isComplete ? "全ステップ完了" : (current?.label ?? "—")}
          </span>
        </div>
      </div>

      <div className="group">
        <div className="group-title">NEXT</div>
        {next ? (
          <div className="hstack">
            <span className="shrink-0 text-fg-dim tabular-nums">#{stepIndex + 2}</span>
            {next.require_trigger ? <span className="shrink-0">✋</span> : null}
            <span className="truncate">{next.label}</span>
          </div>
        ) : (
          <div className="text-fg-dim">これが最終ステップです</div>
        )}
      </div>
    </Panel>
  );
}
