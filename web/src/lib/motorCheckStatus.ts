import type { MotorCheckSnapshot } from "@/lib/protocol";

/**
 * 統合動作確認が今どの状態にあるか。`lib/sequenceStatus.ts` と同じ形で、
 * **判定はここ 1 箇所**、ラベルと配色は画面ごとに決める。
 *
 * かつて判定は `MotorCheckPanel` と `MotorCheckSummary` に別々に書かれており、
 * 実際に食い違っていた。パネル側は「実行中でなく、エラーも無く、ステップ表が
 * 届いている」を完了と読むため、**一度も実行していない状態が「完了」**になり、
 * 全ステップに緑の ✓ が付いた。実配信のスナップショット
 * (`running:false, step_index:0, total_steps:2`) がまさにその形である。
 *
 * `config/checklist.yaml` には「アクチュエータ動作確認 完了」の指差喚呼項目が
 * あり、これは誤表示のままチェックが付く経路だった。
 */
export type MotorCheckOutcome = "idle" | "running" | "failed" | "done";

export interface MotorCheckStatus {
  outcome: MotorCheckOutcome;
  /**
   * 完了したステップ数。**未実行は 0。**
   * 中断・失敗のときは止まった位置までを数える (そこまでは実際に通っている)。
   */
  completedSteps: number;
  /**
   * 起動できない理由。null なら押せる。
   *
   * **切断中だけは画面側でしか分からない** (サーバーへ届かないので理由も返らない)。
   * それ以外の可否はサーバーの `blocked_reason` が正で、UI は導出し直さない。
   */
  reasonLabel: string | null;
  /**
   * 直近の失敗理由 (表示 1 行)。無ければ null。
   *
   * サーバーは `error` (表示用の文) と `last_error` (どのステップで失敗したか) の
   * 2 欄で同じ失敗を言うので、**表示はここ 1 つに畳む**
   * (両方を出すと同じ失敗が画面に 2 度並ぶ)。
   */
  failureReason: string | null;
}

/** 完走判定。ステップ数 0 は「未読込」であって完了ではない */
function isComplete(state: MotorCheckSnapshot): boolean {
  return state.total_steps > 0 && state.step_index >= state.total_steps;
}

export function motorCheckStatus(state: MotorCheckSnapshot, connected: boolean): MotorCheckStatus {
  const reasonLabel = connected ? state.blocked_reason : "切断中のため不可";
  // どちらの欄で届いても「完了していない」を意味する。片方だけを見ると、
  // サーバーが理由の置き場所を変えた瞬間に失敗が「未実行」と同じ表示へ落ちる
  // (動作確認が失敗しても画面が黙る、という元の壊れ方そのもの)。
  // `error` はステップ名まで含んだ表示 1 行なので、あれば優先する
  const failureReason = state.error ?? state.last_error?.message ?? null;

  if (state.running) {
    return { outcome: "running", completedSteps: state.step_index, reasonLabel, failureReason };
  }
  if (failureReason) {
    return { outcome: "failed", completedSteps: state.step_index, reasonLabel, failureReason };
  }
  if (isComplete(state)) {
    return { outcome: "done", completedSteps: state.total_steps, reasonLabel, failureReason };
  }
  return { outcome: "idle", completedSteps: 0, reasonLabel, failureReason };
}
