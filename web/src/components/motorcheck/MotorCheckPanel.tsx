import { Check, Play, Square, TriangleAlert } from "lucide-react";

import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { Modal } from "@/components/ui/Modal";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { useMotorCheck } from "@/hooks/useMotorCheck";
import { cx } from "@/lib/cx";
import { TONE_PROGRESS_CLASS } from "@/lib/tone";

interface MotorCheckPanelProps {
  isOpen: boolean;
  onOpenChange: (open: boolean) => void;
}

/**
 * 統合動作確認の進捗パネル。**両ハンドで 1 つ**なので robot を取らない。
 *
 * 出すのはシーケンスのステップ一覧と、今どこを走っているか。
 * かつてはモータごとの合否表 (期待値 / 観測値) を並べていたが、判定は
 * シーケンスエンジンが担うようになり、失敗はシーケンスが止まる形で現れる
 * (`SequenceTimeoutError` / `AxisSyncError`)。**「合格」の列は無い** —
 * 到達判定を持たない軸 (duty / on_off) にそれを出すと、動いたかどうかを
 * 機械が見ていないのに見たように読めてしまう。
 */
export function MotorCheckPanel({ isOpen, onOpenChange }: MotorCheckPanelProps) {
  const { state, start, abort } = useMotorCheck();

  const total = state.total_steps;
  const done = state.running ? state.step_index : total > 0 && !state.error ? total : 0;
  const percent = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0;

  const footerLabel = state.running
    ? "実行中..."
    : state.error
      ? "中断・失敗"
      : done > 0 && done === total
        ? "完了"
        : "未実行";

  return (
    <Modal
      open={isOpen}
      onClose={() => onOpenChange(false)}
      tone="danger"
      title="アクチュエータ動作確認"
      boxClassName="min-w-[min(560px,80vw)]"
      bodyClassName="flex flex-col gap-3"
      footer={
        <div className="flex w-full items-center justify-between">
          <span className="text-base-content/70">{footerLabel}</span>
          <div className="flex gap-2">
            {state.running ? (
              <Button tone="danger" onClick={abort}>
                <Icon as={Square} />
                中断
              </Button>
            ) : (
              <Button tone="info" disabled={state.blocked_reason !== null} onClick={start}>
                <Icon as={Play} />
                {done > 0 ? "もう一度実行" : "実行"}
              </Button>
            )}
            <Button onClick={() => onOpenChange(false)}>閉じる</Button>
          </div>
        </div>
      }
    >
      {state.running ? (
        <div className="flex flex-col gap-1">
          <div className="flex items-center justify-between">
            <span className="font-mono text-base-content/70 tabular-nums">
              {done} / {total}
            </span>
            <span className="min-w-0 truncate text-info">{state.current_step ?? "—"}</span>
          </div>
          <progress
            className={cx(
              "progress h-[0.7rem] w-full border border-base-300 bg-base-200",
              TONE_PROGRESS_CLASS.info,
            )}
            value={percent}
            max={100}
          />
        </div>
      ) : null}

      {state.error ? (
        <div className="text-error">
          <p className="flex items-center gap-1.5 font-medium">
            <Icon as={TriangleAlert} />
            動作確認は完了していません
          </p>
          <p className="mt-1">{state.error}</p>
        </div>
      ) : null}

      {state.steps.length === 0 ? (
        <p className="px-1 py-3 text-base-content/70">
          {state.available
            ? "動作確認のステップが読み込まれていません。"
            : "この構成では動作確認を実行できません (位置定数が揃っていません)。"}
        </p>
      ) : (
        <ol className="flex flex-col">
          {state.steps.map((step) => {
            const isCurrent = state.running && step.index === state.step_index;
            const isDone = step.index < done;
            return (
              <li
                key={step.index}
                className={cx(
                  "flex items-center gap-2 border-l-2 border-transparent px-2 py-[0.35rem]",
                  isCurrent && "border-l-info bg-base-200 font-medium",
                  isDone && "text-base-content/45",
                )}
              >
                <span className="w-6 shrink-0 text-right font-mono text-base-content/50 tabular-nums">
                  {step.index + 1}
                </span>
                <span className="min-w-0 flex-1 truncate">{step.label}</span>
                {isCurrent ? (
                  <StatusBadge tone="info">実行中</StatusBadge>
                ) : isDone ? (
                  <Icon as={Check} className="shrink-0 text-success" />
                ) : null}
              </li>
            );
          })}
        </ol>
      )}
    </Modal>
  );
}
