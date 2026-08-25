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

/**
 * ヘッダー帯のフェーズ表現。
 * 帯全面をフェーズ色で塗ると画面で最も明るい面になってしまうため、
 * 地はグレーに固定し、左端のバー色だけでフェーズを示す。
 */
export const PHASE_BAND_CLASS: Record<MatchPhase, string> = {
  setup: "border-l-warning",
  ready: "border-l-info",
  match: "border-l-success",
  finished: "border-l-fg-dim",
};

export const PHASE_TEXT_CLASS: Record<MatchPhase, string> = {
  setup: "text-warning",
  ready: "text-info",
  match: "text-success",
  finished: "text-fg-dim",
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
