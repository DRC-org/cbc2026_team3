import type { RobotState } from "@/lib/protocol";

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
