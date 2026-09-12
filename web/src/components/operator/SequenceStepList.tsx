import { Check, ChevronRight, Circle, Hand, Pause, TriangleAlert } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { Modal } from "@/components/ui/Modal";
import { cx } from "@/lib/cx";
import type { SequenceStepInfo } from "@/lib/protocol";
import { scrollWithinContainer } from "@/lib/scrollWithin";

interface SequenceStepListProps {
  steps: SequenceStepInfo[];
  stepIndex: number;
  waitingTrigger: boolean;
  onJump: (index: number) => void;
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

const STEP_MARKER = {
  done: Check,
  current: ChevronRight,
  waiting: Pause,
  future: Circle,
} as const;

const STEP_TONE_CLASS: Record<StepKind, string> = {
  done: "text-base-content/45",
  current: "text-info",
  waiting: "text-warning",
  future: "",
};

const STEP_ACTIVE_CLASS: Record<StepKind, string> = {
  done: "",
  current: "border-l-info bg-base-200 font-medium",
  waiting: "border-l-warning bg-base-200 font-medium",
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
  const currentRef = useRef<HTMLLIElement | null>(null);

  useEffect(() => {
    scrollWithinContainer(currentRef.current, "center");
  }, [stepIndex]);

  if (steps.length === 0) {
    return <p className="text-base-content/70">ステップ情報なし</p>;
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
    <div className="flex min-h-0 flex-1 flex-col">
      <ol className="scroll min-h-0 flex-1">
        {steps.map((step, i) => {
          const kind = classifyStep(i, stepIndex, totalSteps, waitingTrigger);
          const isActive = kind === "current" || kind === "waiting";
          return (
            <li key={step.index} ref={isActive ? currentRef : undefined}>
              <button
                type="button"
                onClick={() => handleRequestJump(i)}
                disabled={disabled}
                aria-current={isActive ? "step" : undefined}
                aria-label={`ステップ ${i + 1}: ${step.label}`}
                title={step.label}
                className={cx(
                  "flex w-full cursor-pointer items-center gap-2 border-l-2 border-transparent px-1 py-[0.15rem] text-left",
                  "enabled:hover:bg-base-200 disabled:cursor-not-allowed",
                  STEP_TONE_CLASS[kind],
                  STEP_ACTIVE_CLASS[kind],
                )}
              >
                <Icon
                  as={STEP_MARKER[kind]}
                  className={cx("text-[0.9em]", kind === "future" && "opacity-40")}
                />
                <span className="w-6 shrink-0 font-mono text-base-content/45 tabular-nums">
                  {i + 1}
                </span>
                <span className="min-w-0 flex-1 truncate">{step.label}</span>
                <span className="w-4 shrink-0">
                  {step.require_trigger ? (
                    <Icon as={Hand} className="text-[0.9em] text-base-content/50" />
                  ) : null}
                </span>
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
        <p className="mt-2 text-base-content/70">
          現在の動作を中断して指定ステップから実行を開始します。
        </p>
        <p className="mt-2 flex items-center gap-1.5 text-warning">
          <Icon as={TriangleAlert} />
          物理状態が安全であることを必ず確認してください。
        </p>
      </Modal>
    </div>
  );
}
