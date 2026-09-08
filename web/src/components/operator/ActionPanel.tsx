import { ArrowRight, Ban, Hand, Play, Square, TriangleAlert } from "lucide-react";

import { TriggerButton } from "@/components/operator/TriggerButton";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { Kbd } from "@/components/ui/Kbd";
import { Panel } from "@/components/ui/Panel";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { cx } from "@/lib/cx";
import type { RobotState } from "@/lib/protocol";
import {
  isRestartFromTop,
  isSequenceComplete,
  sequenceKind,
  sequenceProgress,
} from "@/lib/sequenceStatus";
import type { Tone } from "@/lib/tone";
import { TONE_PROGRESS_CLASS } from "@/lib/tone";

interface ActionPanelProps {
  state: RobotState;
  inMatch: boolean;
  blockedLabel: string;
  /**
   * 試合中でも送れない理由 (切断中など)。null なら送れる。
   *
   * **フェーズによる不可 (`blockedLabel`) とは別軸。** 切断は画面側でしか
   * 分からず、塞がないと「押したのに何も起きない」だけが操縦者に残る。
   */
  blockedReason: string | null;
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
 * 走る範囲を 1 行で予告するのは、NEXT を押した後に機体が止まるのか動き続けるのかを
 * **押す前に**知る必要があるため。これが分からないと操縦者は毎回機体の動きが
 * 終わるまで身構えることになる。ステップの並びそのものは下の一覧が描くので、
 * ここは「何ステップ走ってどこで止まるか」だけに絞る。
 */
export function ActionPanel({
  state,
  inMatch,
  blockedLabel,
  blockedReason,
  onStart,
  onStop,
  onTrigger,
}: ActionPanelProps) {
  const { total_steps: totalSteps, step_index: stepIndex } = state;
  const steps = state.steps ?? [];
  const blocked = blockedReason !== null;

  // 実行状態はサーバーの running を唯一の根拠にする (step_index からの推測をしない)
  const kind = sequenceKind(state);
  // START が「先頭へ戻して全工程を走り直す」意味になっている状態。判定は
  // `lib/sequenceStatus.ts` が持ち、確認の要否 (RobotControl) と必ず同じ条件で動く
  const restartFromTop = isRestartFromTop(state);
  const isComplete = isSequenceComplete(state);
  // 止められるのは動いているときだけ。トリガー待ちもシーケンスは生きている
  const canStop = kind === "running" || kind === "waiting_trigger";
  const { displayIndex, percent, current } = sequenceProgress(state);
  // NEXT 後に走る一連のステップ。次の許可待ち (require_trigger) を含めてそこで切る
  const burst: typeof steps = [];
  if (!isComplete) {
    for (let i = stepIndex + 1; i < steps.length; i += 1) {
      burst.push(steps[i]);
      if (steps[i].require_trigger) break;
    }
  }
  const burstEnd = burst.at(-1);
  // 許可待ちで切れたのか、切れずに終端まで来たのか。**この 2 つを同じ文言にしては
  // ならない** — 後者は押したら最後まで止まらないという別の事実である
  const stopsAtTrigger = burstEnd?.require_trigger ?? false;
  const burstMessage = isComplete
    ? "シーケンスは終了しています"
    : burstEnd === undefined
      ? "これが最終ステップです"
      : stopsAtTrigger
        ? `${burst.length} ステップ走って「${burstEnd.label}」で停止`
        : `残り ${burst.length} ステップを最後まで走り切ります (途中で止まりません)`;
  // START を出すのは「開始できる」ときだけ。ステップが 1 件も無い (no_sequence) を
  // ここへ含めると、開始しようのないシーケンスの START を押させることになる
  const idle = inMatch && kind === "idle";

  // 状態表示と主操作 (TriggerButton) は同じ kind から作る。どちらかを暗黙の
  // フォールバックに任せると、同じ画面が相反する 2 つの事実を出す
  //
  // 中断位置から押す START は先頭へ戻るので、そこだけ「待機中 — START で開始」と
  // 言ってはならない。表示は中断位置 (8/13) を出したまま、押すと全工程が走り直す ——
  // 表示と動作が食い違う唯一の経路だった
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
            : restartFromTop
              ? { label: "停止中 — START は先頭から走り直します", tone: "warning" }
              : { label: "待機中 — START で開始", tone: "neutral" };

  return (
    // 周辺視野でも状態の変化に気付けるよう、左端を状態色で塗る (Panel が引く)
    <Panel accentTone={status.tone} className="shrink-0" bodyClassName="p-0">
      {/* 状態と進捗を 1 行に畳む。別々のパネルに分けると同じことを 2 度読ませる。
          ステップ番号はこの行に置かない — すぐ下の巨大表示が同じ数字を持っている */}
      <div className="flex shrink-0 items-center gap-2 border-b border-base-300 px-2 py-1">
        <StatusBadge tone={status.tone}>{status.label}</StatusBadge>
      </div>
      <progress
        className={cx(
          "progress h-[0.35rem] w-full shrink-0 rounded-none bg-base-200",
          TONE_PROGRESS_CLASS[status.tone],
        )}
        value={percent}
        max={100}
      />

      {/* シーケンスが落ちた理由。**平常時は 1 ピクセルも出さない。**
          これが無い間、左右ずれ検出で止まっても画面は「待機中」へ戻るだけで、
          偏差監視の第 1 段が操縦者から無音だった (押し直せば直ると読める) */}
      {state.last_error ? (
        <div className="flex shrink-0 items-start gap-1.5 border-l-[0.25rem] border-l-error bg-error/5 px-3 py-1">
          <Icon as={TriangleAlert} className="mt-[0.2em] shrink-0 text-error" />
          <span className="min-w-0">
            {/* どのステップで落ちたかを先に出す。理由だけでは、どこまで動いて
                止まったのか (= 今の機体の姿勢) が操縦者に分からない */}
            <span className="mr-2 font-medium">
              ステップ {state.last_error.step_index + 1}「{state.last_error.step}」で停止
            </span>
            <span className="text-base-content/80">{state.last_error.message}</span>
          </span>
        </div>
      ) : null}

      {/* 現在ステップ。視線を戻した一瞬で読めることだけが要件なので、
          画面で最も大きい文字にする */}
      <div className="flex min-h-[5.5rem] shrink-0 items-center px-4 py-3">
        <div className="flex min-w-0 items-baseline gap-4">
          <span className="shrink-0 font-mono text-[3em] leading-none text-base-content/30 tabular-nums">
            {displayIndex}
            {/* 総数は「あとどれだけ残っているか」の目安でしかないので、
                一瞬で読む必要がある現在番号より一回り小さく添える */}
            <span className="text-[0.45em] text-base-content/40">/{totalSteps}</span>
          </span>
          <span className="min-w-0 text-[3em] leading-[1.1] font-semibold">
            {isComplete ? "全ステップ完了" : (current?.label ?? "—")}
          </span>
        </div>
      </div>

      {/* 押すと何が起きるか。NEXT を押すと機体は次の許可待ちまで複数ステップを
          一気に走るので、「何ステップ走ってどこで止まるか」を押す前に確定させる。
          ステップの並びは下の一覧が描くので、ここで列挙し直さない */}
      <div className="flex shrink-0 items-center gap-2 border-t border-base-300 px-4 py-2">
        <Icon
          as={stopsAtTrigger ? Hand : ArrowRight}
          className={stopsAtTrigger ? "text-warning" : "text-base-content/60"}
        />
        <span className="min-w-0 truncate text-base-content/80">{burstMessage}</span>
      </div>

      {/* 主操作。右の大きい面が常に「今押すべきボタン」で、左は常に停止。
          状態によって位置が入れ替わると、押す直前に毎回探し直すことになる */}
      <div className="grid min-h-[5.5rem] shrink-0 grid-cols-[minmax(9rem,0.28fr)_1fr] gap-px border-t border-base-300 bg-base-300">
        {/* 通常停止は安全側の動作。確認ダイアログを挟むと「止めたいのに止まらない」
            時間が生まれるため、ここは 1 アクションで即座に止める */}
        <Button
          tone="danger"
          disabled={!inMatch || !canStop || blocked}
          onClick={onStop}
          aria-label="シーケンスを通常停止"
          className={PRIMARY_CLASS}
        >
          <Icon as={Square} />
          STOP
        </Button>

        {idle ? (
          <Button
            tone={restartFromTop ? "warn" : "ok"}
            disabled={blocked}
            onClick={onStart}
            aria-label={
              blocked
                ? `操作不可: ${blockedReason}`
                : restartFromTop
                  ? "シーケンスを先頭から再開"
                  : "シーケンスを先頭から開始"
            }
            className={PRIMARY_CLASS}
          >
            <Icon as={blocked ? Ban : Play} />
            {blocked ? blockedReason : restartFromTop ? "先頭から再開" : "START"}
            {blocked ? null : <Kbd>Space</Kbd>}
          </Button>
        ) : (
          <TriggerButton
            kind={kind}
            onTrigger={onTrigger}
            disabled={!inMatch || blocked}
            disabledLabel={blockedReason ?? blockedLabel}
          />
        )}
      </div>
    </Panel>
  );
}
