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
 * 実行状態はサーバーの `running` をそのまま使う。
 *
 * `step_index === 0 && total_steps > 0` のような推測をしてはならない。準備フェーズでは
 * その条件が常に成立し、動作確認ボタンが「主役であるはずのフェーズ」で常時無効になった
 * (`MotorCheckButton` のコメントがその事故を記録している)。同じ推測は STOP 直後にも
 * 効かず、止まっているのに RUNNING を出し続けていた。
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
 * `sequence_stop` は `step_index` を保持したままシーケンスを降ろすので、画面は
 * 「8/13・現在ステップ○○・待機中」を出したままになる。そこで押す START (と
 * Space 1 打) はステップ 0 へ戻り、**中断姿勢のまま先頭の動作が走る**。
 * 同じ「任意ステップから再開」である `sequence_jump` は確認モーダルを挟むのに、
 * より危険なこちらだけが素通しだった。
 *
 * 判定をここに置くのは、**ボタンの文言 (`ActionPanel`) と確認の要否
 * (`RobotControl` の Space / onStart) が必ず同じ条件で動く**ようにするため。
 * 片方だけに書くと「文言は『先頭から再開』なのに Space は確認なしで走る」が作れる。
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
 * 進捗の算術。**`sequenceKind` / `isSequenceComplete` と同じ理由でここに 1 本だけ置く。**
 *
 * 操縦者の `ActionPanel` と Monitor の `RobotStatusRow` に、完走時の丸め・0 除算の
 * 回避・現在ステップの取り方まで同一の式が写経されていた。判定は既に一本化して
 * あったのにその先の算術だけが分かれており、片方だけ直せば同じ瞬間に 2 つの画面が
 * 違う進捗を出す。
 *
 * **バーの分子は「完了したステップ数」= `step_index` であって、操縦者に見せる現在
 * ステップ番号 (`displayIndex` = `step_index + 1`) ではない。** サーバーはステップを
 * 完了した時点で `step_index` を進めるので、この値がそのまま完了件数を意味する。
 * 両方に同じ式を使うとバーが常に 1 ステップ先行し、シーケンスを開始していない試合
 * 開始直後の画面が「1 マス進んだバー」を出す (実際にそう見えていた)。走行中のステップを
 * 0.5 件のように按分しないのは、そのステップの進み具合を測る手段がどこにも無いため ——
 * 実行中であることは状態表示 (`sequenceKind`) が言う。
 *
 * 完走が 100% になるのも同じ式で足りる。バックエンドは完走で
 * `step_index === total_steps` を返す (= 全件完了)。
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
