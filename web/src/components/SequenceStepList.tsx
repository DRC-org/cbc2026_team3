import { useState } from "react";

import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";
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

// 行全体の文字色。
const STEP_TONE_CLASS: Record<StepKind, string> = {
  done: "text-fg-dim",
  current: "text-info",
  waiting: "text-warning",
  future: "",
};

// 実行位置の行だけ左端にカラーバーと薄い地色を敷き、一覧の中で現在地を見失わせない。
const STEP_ACTIVE_CLASS: Record<StepKind, string> = {
  done: "",
  current: "border-l-info bg-raised",
  waiting: "border-l-warning bg-raised",
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
    return <p className="text-fg-dim">ステップ情報なし</p>;
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
    <div className="flex min-h-0 flex-1 flex-col gap-1">
      <div className="hsplit group-title shrink-0">
        <span>STEP LIST</span>
        <span>{disabled ? "試合中のみ操作可" : "クリックで再開"}</span>
      </div>

      <ol className="panel-body scroll">
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
                  "flex w-full cursor-pointer items-center gap-2 border-l-2 border-transparent px-[0.4rem] py-[0.15rem] text-left",
                  "enabled:hover:bg-raised disabled:cursor-not-allowed",
                  STEP_TONE_CLASS[kind],
                  STEP_ACTIVE_CLASS[kind],
                )}
              >
                <span className="w-4 shrink-0 text-center tabular-nums">{STEP_MARKER[kind]}</span>
                <span className="w-7 shrink-0 text-fg-dim tabular-nums">#{i + 1}</span>
                <span className="w-5 shrink-0 text-center">{step.require_trigger ? "✋" : ""}</span>
                <span className="flex-1 truncate">{step.label}</span>
              </button>
            </li>
          );
        })}
      </ol>

      <Modal
        open={pendingIndex !== null}
        onClose={handleCancel}
        tone="danger"
        title="STEP JUMP"
        footer={
          <>
            <Button onClick={handleCancel}>キャンセル</Button>
            <Button tone="warn" onClick={handleConfirm}>
              再開
            </Button>
          </>
        }
      >
        <p>
          ステップ {pendingIndex !== null ? pendingIndex + 1 : ""}{" "}
          {target ? `「${target.label}」` : ""} から再開しますか？
        </p>
        <p className="mt-2 text-fg-dim">現在の動作を中断して指定ステップから実行を開始します。</p>
        <p className="mt-2 text-warning">⚠ 物理状態が安全であることを必ず確認してください。</p>
      </Modal>
    </div>
  );
}
