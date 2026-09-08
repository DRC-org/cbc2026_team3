import type { RobotState, SequenceStepInfo } from "@/lib/protocol";

/**
 * シーケンスが今どの状態にあるか。ラベルや配色は画面ごとに違う
 * (操縦者には「待機中 — START で開始」、Monitor には「待機中」) が、
 * **どの状態か**の判定はここ 1 箇所で行う。
 */
export type SequenceKind = "no_sequence" | "idle" | "waiting_trigger" | "running" | "complete";

type Progress = Pick<RobotState, "running" | "waiting_trigger" | "step_index" | "total_steps">;

/**
 * 完走判定。バックエンドは完走時に `step_index === total_steps` を返す。
 * トリガー待ちの間は次のステップが残っているので完走ではない。
 */
export function isSequenceComplete(state: Progress): boolean {
  return state.total_steps > 0 && state.step_index >= state.total_steps && !state.waiting_trigger;
}

/**
 * 実行状態はサーバーの `running` をそのまま使う。`step_index === 0 && total_steps > 0`
 * のような推測をしてはならない —— 準備フェーズではその条件が常に成立して動作確認ボタンが
 * 常時無効になり、STOP 直後には止まっているのに RUNNING を出し続ける。
 */
export function sequenceKind(state: Progress): SequenceKind {
  if (state.total_steps === 0) return "no_sequence";
  // 押すべきボタンが NEXT に変わるので、実行中フラグより先に判定する
  if (state.waiting_trigger) return "waiting_trigger";
  if (state.running) return "running";
  if (isSequenceComplete(state)) return "complete";
  return "idle";
}

/**
 * START が「先頭へ戻して全工程を走り直す」意味になる状態か。
 *
 * `sequence_stop` は `step_index` を保持したまま降りるので、画面は「8/13・待機中」を
 * 出したままになる。そこで押す START (と Space 1 打) はステップ 0 へ戻り、**中断姿勢の
 * まま先頭の動作が走る**。判定をここに置くのは、**ボタンの文言 (`ActionPanel`) と確認の
 * 要否 (`RobotControl`) が必ず同じ条件で動く**ようにするため —— 片方だけに書くと
 * 「文言は『先頭から再開』なのに Space は確認なしで走る」が作れる。
 */
export function isRestartFromTop(state: Progress): boolean {
  return sequenceKind(state) === "idle" && state.step_index > 0;
}

/** 進捗の算出に要るぶんだけ。ステップ表そのものが要るのは `current` のため */
type ProgressWithSteps = Progress & Pick<RobotState, "steps">;

export interface SequenceProgress {
  /** 操縦者に見せる 1 始まりの番号。ステップが 1 件も無ければ 0 */
  displayIndex: number;
  total: number;
  /** 進捗バーの % (0-100)。完走時は 100 */
  percent: number;
  /** 今いるステップ。完走後は「今いるステップ」が無いので undefined */
  current: SequenceStepInfo | undefined;
}

/**
 * 進捗の算術。**`sequenceKind` / `isSequenceComplete` と同じ理由でここに 1 本だけ置く**
 * —— 操縦者の `ActionPanel` と Monitor の `RobotStatusRow` に同じ式を写経すると、
 * 片方だけ直した瞬間に 2 つの画面が違う進捗を出す。
 *
 * **バーの分子は「完了したステップ数」= `step_index` であって、操縦者に見せる現在
 * ステップ番号 (`displayIndex` = `step_index + 1`) ではない。** サーバーはステップを
 * 完了した時点で `step_index` を進めるので、この値がそのまま完了件数を意味する ——
 * 両方に同じ式を使うとバーが常に 1 ステップ先行し、開始前の画面が「1 マス進んだバー」を
 * 出す。走行中のステップを按分しないのは、進み具合を測る手段がどこにも無いため。
 */
export function sequenceProgress(state: ProgressWithSteps): SequenceProgress {
  const total = state.total_steps;
  const index = state.step_index;
  const complete = isSequenceComplete(state);

  if (total <= 0) return { displayIndex: 0, total, percent: 0, current: undefined };

  return {
    displayIndex: Math.min(index + 1, total),
    total,
    percent: Math.min(100, (index / total) * 100),
    current: complete ? undefined : state.steps?.[index],
  };
}
