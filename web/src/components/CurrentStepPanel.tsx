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
    <div
      className="tui-window"
      style={{ display: "flex", flex: 1, flexDirection: "column", minHeight: 0 }}
    >
      <fieldset
        className="tui-fieldset"
        style={{ display: "flex", flex: 1, flexDirection: "column", gap: "0.75rem", minHeight: 0 }}
      >
        <legend>CURRENT STEP</legend>

        <div
          style={{
            display: "flex",
            flex: 1,
            flexDirection: "column",
            gap: "0.5rem",
            justifyContent: "center",
            minHeight: 0,
          }}
        >
          <div className={status.className}>
            [{status.symbol} {status.label}]
          </div>
          <div style={{ display: "flex", alignItems: "baseline", gap: "0.75rem", minWidth: 0 }}>
            <span
              className="tabular-nums"
              style={{ flexShrink: 0, fontSize: "1.6em", opacity: 0.7 }}
            >
              #{isComplete ? totalSteps : stepIndex + 1}
            </span>
            <span style={{ minWidth: 0, fontSize: "1.6em", lineHeight: 1.25 }}>
              {isComplete ? "全ステップ完了" : (current?.label ?? "—")}
            </span>
          </div>
        </div>

        <div style={{ borderTop: "1px solid rgba(255,255,255,0.3)", paddingTop: "0.5rem" }}>
          <div style={{ opacity: 0.7 }}>NEXT</div>
          {next ? (
            <div style={{ display: "flex", alignItems: "baseline", gap: "0.5rem", minWidth: 0 }}>
              <span className="tabular-nums" style={{ flexShrink: 0, opacity: 0.8 }}>
                #{stepIndex + 2}
              </span>
              {next.require_trigger ? <span style={{ flexShrink: 0 }}>✋</span> : null}
              <span
                style={{
                  minWidth: 0,
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                  whiteSpace: "nowrap",
                }}
              >
                {next.label}
              </span>
            </div>
          ) : (
            <div style={{ opacity: 0.7 }}>これが最終ステップです</div>
          )}
        </div>
      </fieldset>
    </div>
  );
}
