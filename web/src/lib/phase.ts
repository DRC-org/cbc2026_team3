import type { MatchCourt, MatchMode, MatchPhase } from "@/hooks/useRobotSocket";
import type { Tone } from "@/lib/tone";

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

/** フェーズの状態色。ヘッダーのチップと左端バーの双方がこれを引く */
export const PHASE_TONE: Record<MatchPhase, Tone> = {
  setup: "warning",
  ready: "info",
  match: "success",
  finished: "neutral",
};

/**
 * ヘッダー帯の左端バー。
 * 帯全面をフェーズ色で塗ると画面で最も明るい面になってしまうため、
 * 地は白のまま固定し、左端のバー色とチップだけでフェーズを示す。
 */
export const PHASE_BAND_CLASS: Record<MatchPhase, string> = {
  setup: "border-l-warning",
  ready: "border-l-info",
  match: "border-l-success",
  finished: "border-l-base-300",
};

/** コートの状態色。赤/青は誤設定のまま試合に入る事故を防ぐため常時表示する */
export const COURT_TONE: Record<MatchCourt, Tone> = {
  red: "error",
  blue: "info",
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
