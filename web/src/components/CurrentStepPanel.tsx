import { Fieldset } from "@tsaito18/tuicss-react";

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
    ? { label: "シーケンス完走", className: "success-text", symbol: "✓" }
    : waitingTrigger
      ? { label: "許可待ち — NEXT を押してください", className: "warning-text", symbol: "▮" }
      : { label: "実行中", className: "info-text", symbol: "►" };

  return (
    <Fieldset className="panel panel-fill" legend="CURRENT STEP">
      <div
        className="fill"
        style={{
          display: "flex",
          flexDirection: "column",
          gap: "0.5rem",
          justifyContent: "center",
        }}
      >
        <div className={status.className}>
          [{status.symbol} {status.label}]
        </div>
        <div style={{ display: "flex", alignItems: "baseline", gap: "0.75rem", minWidth: 0 }}>
          <span className="dim tabular-nums" style={{ flexShrink: 0, fontSize: "1.6em" }}>
            #{isComplete ? totalSteps : stepIndex + 1}
          </span>
          <span style={{ minWidth: 0, fontSize: "1.6em", lineHeight: 1.25 }}>
            {isComplete ? "全ステップ完了" : (current?.label ?? "—")}
          </span>
        </div>
      </div>

      <div className="group">
        <div className="group-title">NEXT</div>
        {next ? (
          <div className="hstack">
            <span className="dim tabular-nums" style={{ flexShrink: 0 }}>
              #{stepIndex + 2}
            </span>
            {next.require_trigger ? <span style={{ flexShrink: 0 }}>✋</span> : null}
            <span className="ellipsis">{next.label}</span>
          </div>
        ) : (
          <div className="dim">これが最終ステップです</div>
        )}
      </div>
    </Fieldset>
  );
}
