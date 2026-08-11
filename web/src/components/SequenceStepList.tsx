import { Button, Modal, ModalBody, ModalFooter, ModalHeader } from "@tsaito18/tuicss-react";
import { useState } from "react";

import type { SequenceStepInfo } from "@/hooks/useRobotSocket";
import { cx } from "@/lib/cx";

interface SequenceStepListProps {
  steps: SequenceStepInfo[];
  stepIndex: number;
  waitingTrigger: boolean;
  onJump: (index: number) => void;
  /** 試合中以外はステップジャンプを禁止する (サーバー側でも拒否される) */
  disabled?: boolean;
}

type StepKind = "done" | "current" | "waiting" | "future";

function classifyStep(
  i: number,
  stepIndex: number,
  totalSteps: number,
  waitingTrigger: boolean,
): StepKind {
  if (stepIndex >= totalSteps) return "done";
  if (i < stepIndex) return "done";
  if (i === stepIndex) return waitingTrigger ? "waiting" : "current";
  return "future";
}

// 状態別の左端マーカー記号。done=済 / current=実行中 / waiting=許可待ち / future=未到達。
const STEP_MARKER: Record<StepKind, string> = {
  done: "✓",
  current: "►",
  waiting: "▮",
  future: "·",
};

// 行全体の文字色（TuiCss セマンティック text クラス）。
const STEP_TONE_CLASS: Record<StepKind, string> = {
  done: "secondary-text",
  current: "info-text",
  waiting: "warning-text",
  future: "",
};

export function SequenceStepList({
  steps,
  stepIndex,
  waitingTrigger,
  onJump,
  disabled = false,
}: SequenceStepListProps) {
  const [pendingIndex, setPendingIndex] = useState<number | null>(null);

  if (steps.length === 0) {
    return <p style={{ padding: 8, opacity: 0.7 }}>ステップ情報なし</p>;
  }

  const totalSteps = steps.length;
  const target = pendingIndex !== null ? steps[pendingIndex] : null;

  const handleRequestJump = (index: number) => {
    if (disabled || index === stepIndex) return;
    setPendingIndex(index);
  };

  const handleConfirm = () => {
    if (pendingIndex !== null) onJump(pendingIndex);
    setPendingIndex(null);
  };

  const handleCancel = () => setPendingIndex(null);

  return (
    <div className="flex-1" style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      <div
        style={{
          display: "flex",
          flexShrink: 0,
          alignItems: "center",
          justifyContent: "space-between",
        }}
      >
        <h3 style={{ opacity: 0.8 }}>STEP LIST</h3>
        <span style={{ opacity: 0.6 }}>{disabled ? "試合中のみ操作可" : "クリックで再開"}</span>
      </div>

      <ol
        className="tui-scroll-cyan flex-1"
        style={{ display: "flex", flexDirection: "column", overflow: "auto" }}
      >
        {steps.map((step, i) => {
          const kind = classifyStep(i, stepIndex, totalSteps, waitingTrigger);
          const isActive = kind === "current" || kind === "waiting";
          return (
            <li key={step.index}>
              <button
                type="button"
                onClick={() => handleRequestJump(i)}
                disabled={disabled}
                aria-current={isActive ? "step" : undefined}
                aria-label={`ステップ ${i + 1}: ${step.label}`}
                className={cx(
                  STEP_TONE_CLASS[kind],
                  isActive && (kind === "current" ? "info" : "warning"),
                )}
                style={{
                  display: "flex",
                  width: "100%",
                  alignItems: "center",
                  gap: 8,
                  padding: "6px 8px",
                  textAlign: "left",
                  cursor: disabled ? "not-allowed" : "pointer",
                  border: "none",
                  background: "transparent",
                  opacity: disabled ? 0.6 : 1,
                }}
              >
                <span
                  className="tabular-nums"
                  style={{ width: "1rem", flexShrink: 0, textAlign: "center" }}
                >
                  {STEP_MARKER[kind]}
                </span>
                <span
                  className="tabular-nums"
                  style={{ width: "1.75rem", flexShrink: 0, opacity: 0.8 }}
                >
                  #{i + 1}
                </span>
                <span style={{ width: "1.25rem", flexShrink: 0, textAlign: "center" }}>
                  {step.require_trigger ? "✋" : ""}
                </span>
                <span
                  style={{
                    minWidth: 0,
                    flex: 1,
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                    whiteSpace: "nowrap",
                  }}
                >
                  {step.label}
                </span>
              </button>
            </li>
          );
        })}
      </ol>

      <Modal
        open={pendingIndex !== null}
        onClose={handleCancel}
        windowClassName="red-168 left-align"
      >
        <ModalHeader>STEP JUMP</ModalHeader>
        <ModalBody>
          <p>
            ステップ {pendingIndex !== null ? pendingIndex + 1 : ""}{" "}
            {target ? `「${target.label}」` : ""} から再開しますか？
          </p>
          <p style={{ marginTop: 8, opacity: 0.8 }}>
            現在の動作を中断して指定ステップから実行を開始します。
          </p>
          <p className="warning-text" style={{ marginTop: 8 }}>
            ⚠ 物理状態が安全であることを必ず確認してください。
          </p>
        </ModalBody>
        <ModalFooter>
          <Button onClick={handleCancel}>キャンセル</Button>
          <Button className="yellow-255" onClick={handleConfirm}>
            再開
          </Button>
        </ModalFooter>
      </Modal>
    </div>
  );
}
