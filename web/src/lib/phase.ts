import type { MatchCourt, MatchMode, MatchPhase } from "@/hooks/useRobotSocket";

export const MODE_LABEL: Record<MatchMode, string> = {
  semi_auto: "半自動",
  full_auto: "全自動",
};

export const COURT_LABEL: Record<MatchCourt, string> = {
  red: "赤コート",
  blue: "青コート",
};

export const PHASE_LABEL: Record<MatchPhase, string> = {
  setup: "セッティングタイム",
  ready: "試合開始待ち",
  match: "試合中",
  finished: "試合終了",
};

/** フェーズ帯の背景色。一瞥で「今 動くのか動かないのか」を判別させるのが目的。 */
export const PHASE_BAND_CLASS: Record<MatchPhase, string> = {
  setup: "yellow-168 black-255-text",
  ready: "cyan-168 black-255-text",
  match: "green-168 black-255-text",
  finished: "white-168 black-255-text",
};

export const PHASE_TEXT_CLASS: Record<MatchPhase, string> = {
  setup: "yellow-255-text",
  ready: "cyan-255-text",
  match: "green-255-text",
  finished: "white-255-text",
};

/**
 * 準備フェーズ (セッティングタイム)。
 * setup と ready はチェックリストの完了状況で自動遷移する連続した準備期間なので、
 * 画面レイアウトの出し分けでは 1 つのフェーズとして扱う。
 */
export function isSetupPhase(phase: MatchPhase): boolean {
  return phase === "setup" || phase === "ready";
}

/** 試合フェーズ。finished は結果確認のため試合中と同じ情報密度を保つ。 */
export function isMatchPhase(phase: MatchPhase): boolean {
  return phase === "match" || phase === "finished";
}
