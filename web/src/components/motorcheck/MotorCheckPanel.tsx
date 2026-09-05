import { Check, CircleHelp, ListMinus, Play, Square, TriangleAlert } from "lucide-react";

import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { Modal } from "@/components/ui/Modal";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { useRobotStatus } from "@/context/RobotContext";
import { useMotorCheck } from "@/hooks/useMotorCheck";
import { cx } from "@/lib/cx";
import { motorCheckStatus } from "@/lib/motorCheckStatus";
import type { MotorCheckOutcome } from "@/lib/motorCheckStatus";
import { MALFORMED } from "@/lib/protocol";
import { TONE_PROGRESS_CLASS } from "@/lib/tone";

const FOOTER_LABEL: Record<MotorCheckOutcome, string> = {
  running: "実行中...",
  failed: "中断・失敗",
  done: "完了",
  idle: "未実行",
};

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
  const { connected } = useRobotStatus();
  const { state, start, abort } = useMotorCheck();

  // 完了判定は `lib/motorCheckStatus.ts` の 1 箇所だけが持つ。ここで書き直すと
  // 同じ瞬間にパネルは「完了」、サマリーは「未実行」を出す状態が戻る
  const {
    outcome,
    completedSteps: done,
    reasonLabel,
    failureReason,
  } = motorCheckStatus(state, connected);
  const total = state.total_steps;
  const percent = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0;

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
          <span className="flex min-w-0 items-center gap-2 text-base-content/70">
            {FOOTER_LABEL[outcome]}
            {/* Tooltip が使えないので無効化理由はテキストで併記する。
                起動ボタンと同じ文言・同じ判定を使う (両者で導出すると食い違う) */}
            {reasonLabel && outcome !== "running" ? (
              <span className="flex min-w-0 items-center gap-1.5">
                <Icon as={CircleHelp} />
                <span className="min-w-0 truncate">{reasonLabel}</span>
              </span>
            ) : null}
          </span>
          <div className="flex gap-2">
            {state.running ? (
              <Button tone="danger" onClick={abort}>
                <Icon as={Square} />
                中断
              </Button>
            ) : (
              <Button tone="info" disabled={reasonLabel !== null} onClick={start}>
                <Icon as={Play} />
                {outcome === "idle" ? "実行" : "もう一度実行"}
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

      {/* 失敗理由はサーバーが `error` / `last_error` の 2 欄で言ってくるので、
          `motorCheckStatus` が畳んだ 1 つだけを出す (両方出すと同じ 1 行が 2 度並ぶ) */}
      {failureReason ? (
        <div className="text-error">
          <p className="flex items-center gap-1.5 font-medium">
            <Icon as={TriangleAlert} />
            動作確認は完了していません
          </p>
          <p className="mt-1">{failureReason}</p>
        </div>
      ) : null}

      {/* **除外は必ず出す。** 出さないと、サブハンド不在でステップが減っているのか、
          本番構成なのに config の書き忘れで減っているのかを操縦者が区別できない
          (どちらも「全ステップ成功」として同じに見える) */}
      {state.excluded_steps === MALFORMED ? (
        <div className="text-warning">
          <p className="flex items-center gap-1.5 font-medium">
            <Icon as={TriangleAlert} />
            除外ステップを読み取れませんでした
          </p>
          <p className="mt-1">
            ステップ一覧が全てを表しているとは限りません (配信の形が読めていません)。
          </p>
        </div>
      ) : state.excluded_steps.length > 0 ? (
        <div className="rounded-sm border border-warning/40 bg-warning/10 px-3 py-2">
          <p className="flex items-center gap-1.5 font-medium text-warning">
            <Icon as={ListMinus} />
            この構成に無い軸のステップを {state.excluded_steps.length} 件除外しています
          </p>
          <ul className="mt-1 flex flex-col gap-0.5">
            {state.excluded_steps.map((excluded) => (
              <li key={excluded.step} className="flex flex-wrap items-baseline gap-x-2">
                <span className="text-base-content/80">{excluded.step}</span>
                <span className="font-mono text-[0.85em] text-base-content/60">
                  軸が無い: {excluded.missing_axes.join(", ")}
                </span>
              </li>
            ))}
          </ul>
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
