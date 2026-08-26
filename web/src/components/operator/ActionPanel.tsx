import { ArrowRight, Hand, Play, Square } from "lucide-react";

import { TriggerButton } from "@/components/operator/TriggerButton";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { Kbd } from "@/components/ui/Kbd";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { cx } from "@/lib/cx";
import type { RobotState } from "@/lib/protocol";
import { isSequenceComplete, sequenceKind } from "@/lib/sequenceStatus";
import type { Tone } from "@/lib/tone";
import { TONE_BORDER_L_CLASS, TONE_PROGRESS_CLASS } from "@/lib/tone";

interface ActionPanelProps {
  state: RobotState;
  inMatch: boolean;
  blockedLabel: string;
  onStart: () => void;
  onStop: () => void;
  onTrigger: () => void;
}

// 主操作ボタンの共通寸法。状態が変わっても位置とサイズを動かさない（探させない）
const PRIMARY_CLASS = "h-full w-full rounded-none border-0 text-[1.3em]";

/**
 * 試合中の主役。「今なにをすべきか」と「押すと何が起きるか」だけを答える。
 *
 * 以前は同じ事実が 3 箇所に描かれていた — SEQUENCE パネルの `4/13 ステップ名`、
 * CURRENT STEP パネルの `4 ステップ名`、STEP 一覧のハイライト。
 * 操縦者は 3 回読んでようやく 1 つの事実にたどり着いていた。ここに一本化する。
 *
 * 次ステップと ✋ (許可待ちの有無) を併記するのは、NEXT を押した後に機体が
 * 止まるのか動き続けるのかを**押す前に**知る必要があるため。これが分からないと
 * 操縦者は毎回機体の動きが終わるまで身構えることになる。
 */
export function ActionPanel({
  state,
  inMatch,
  blockedLabel,
  onStart,
  onStop,
  onTrigger,
}: ActionPanelProps) {
  const { total_steps: totalSteps, step_index: stepIndex } = state;
  const steps = state.steps ?? [];

  // 実行状態はサーバーの running を唯一の根拠にする (step_index からの推測をしない)
  const kind = sequenceKind(state);
  const isComplete = isSequenceComplete(state);
  // 止められるのは動いているときだけ。トリガー待ちもシーケンスは生きている
  const canStop = kind === "running" || kind === "waiting_trigger";
  const current = isComplete ? null : steps[stepIndex];
  // NEXT 後に走る一連のステップ。次の許可待ち (require_trigger) を含めてそこで切る。
  // 許可待ちが無いまま延々続く場合に画面を埋めないよう表示は数件で打ち切る
  const UPCOMING_LIMIT = 4;
  const burst: typeof steps = [];
  if (!isComplete) {
    for (let i = stepIndex + 1; i < steps.length; i += 1) {
      burst.push(steps[i]);
      if (steps[i].require_trigger) break;
    }
  }
  const upcoming = burst.slice(0, UPCOMING_LIMIT);
  const moreCount = burst.length - upcoming.length;
  // START を出すのは「開始できる」ときだけ。ステップが 1 件も無い (no_sequence) を
  // ここへ含めると、開始しようのないシーケンスの START を押させることになる
  const idle = inMatch && kind === "idle";

  const displayIndex = totalSteps > 0 ? Math.min(stepIndex + 1, totalSteps) : 0;
  const percent =
    totalSteps > 0
      ? Math.min(100, ((isComplete ? totalSteps : stepIndex + 1) / totalSteps) * 100)
      : 0;

  // 状態表示と主操作 (TriggerButton) は同じ kind から作る。どちらかを暗黙の
  // フォールバックに任せると、同じ画面が相反する 2 つの事実を出す
  const status: { label: string; tone: Tone } = !inMatch
    ? { label: blockedLabel, tone: "neutral" }
    : kind === "no_sequence"
      ? { label: "シーケンス未取得", tone: "neutral" }
      : kind === "complete"
        ? { label: "完走", tone: "success" }
        : kind === "waiting_trigger"
          ? { label: "許可待ち — NEXT を押してください", tone: "warning" }
          : kind === "running"
            ? { label: "実行中", tone: "info" }
            : { label: "待機中 — START で開始", tone: "neutral" };

  return (
    <section
      className={cx(
        "card card-border flex shrink-0 flex-col border-base-300 border-l-[0.4rem] bg-base-100",
        // 周辺視野でも状態の変化に気付けるよう、左端を状態色で塗る
        TONE_BORDER_L_CLASS[status.tone],
      )}
    >
      {/* 状態と進捗を 1 行に畳む。別々のパネルに分けると同じことを 2 度読ませる */}
      <div className="flex shrink-0 items-center gap-2 border-b border-base-300 px-2 py-1">
        <StatusBadge tone={status.tone}>{status.label}</StatusBadge>
        <span className="ml-auto shrink-0 font-mono text-base-content/70 tabular-nums">
          {displayIndex}
          <span className="text-base-content/45">/{totalSteps}</span>
        </span>
      </div>
      <progress
        className={cx(
          "progress h-[0.35rem] w-full shrink-0 rounded-none bg-base-200",
          TONE_PROGRESS_CLASS[status.tone],
        )}
        value={percent}
        max={100}
      />

      {/* 現在ステップ。視線を戻した一瞬で読めることだけが要件なので、
          画面で最も大きい文字にする */}
      <div className="flex min-h-[5.5rem] shrink-0 items-center px-4 py-3">
        <div className="flex min-w-0 items-baseline gap-4">
          <span className="shrink-0 font-mono text-[3em] leading-none text-base-content/30 tabular-nums">
            {displayIndex}
          </span>
          <span className="min-w-0 text-[3em] leading-[1.1] font-semibold">
            {isComplete ? "全ステップ完了" : (current?.label ?? "—")}
          </span>
        </div>
      </div>

      {/* 押すと何が起きるか。
          NEXT を押すと機体は次の許可待ちまで複数ステップを一気に走る。
          「次の 1 件」だけ出しても、どこまで動いて止まるのかが分からない。
          停止点までのまとまりを予告して、操縦者が身構える範囲を確定させる */}
      <div className="flex min-h-0 shrink-0 flex-col gap-1 border-t border-base-300 px-4 py-2">
        <span className="text-[0.85em] tracking-wide text-base-content/60">
          {isComplete ? "この先の動作" : "NEXT で走る範囲"}
        </span>
        {upcoming.length === 0 ? (
          <span className="text-base-content/60">
            {isComplete ? "シーケンスは終了しています" : "これが最終ステップです"}
          </span>
        ) : (
          <ol className="flex flex-col">
            {upcoming.map((step, i) => (
              <li key={step.index} className="flex min-w-0 items-center gap-2 text-[1.05em]">
                <Icon
                  as={ArrowRight}
                  className={cx(i === 0 ? "text-base-content/60" : "text-transparent")}
                />
                <span className="shrink-0 font-mono text-base-content/60 tabular-nums">
                  {step.index + 1}
                </span>
                <span className="min-w-0 truncate">{step.label}</span>
                {step.require_trigger ? (
                  <span className="ml-auto flex shrink-0 items-center gap-1 font-medium whitespace-nowrap text-warning">
                    <Icon as={Hand} />
                    ここで停止
                  </span>
                ) : null}
              </li>
            ))}
            {moreCount > 0 ? (
              <li className="pl-8 text-base-content/50">…さらに {moreCount} ステップ</li>
            ) : null}
          </ol>
        )}
      </div>

      {/* 主操作。右の大きい面が常に「今押すべきボタン」で、左は常に停止。
          状態によって位置が入れ替わると、押す直前に毎回探し直すことになる */}
      <div className="grid min-h-[5.5rem] shrink-0 grid-cols-[minmax(9rem,0.28fr)_1fr] gap-px border-t border-base-300 bg-base-300">
        {/* 通常停止は安全側の動作。確認ダイアログを挟むと「止めたいのに止まらない」
            時間が生まれるため、ここは 1 アクションで即座に止める */}
        <Button
          tone="danger"
          disabled={!inMatch || !canStop}
          onClick={onStop}
          aria-label="シーケンスを通常停止"
          className={PRIMARY_CLASS}
        >
          <Icon as={Square} />
          STOP
        </Button>

        {idle ? (
          <Button
            tone="ok"
            onClick={onStart}
            aria-label="シーケンスを先頭から開始"
            className={PRIMARY_CLASS}
          >
            <Icon as={Play} />
            START
            <Kbd>Space</Kbd>
          </Button>
        ) : (
          <TriggerButton
            kind={kind}
            onTrigger={onTrigger}
            disabled={!inMatch}
            disabledLabel={blockedLabel}
          />
        )}
      </div>
    </section>
  );
}
