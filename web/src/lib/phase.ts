import { MALFORMED } from "@/lib/protocol";
import type { Malformed, MatchCourt, MatchPhase } from "@/lib/protocol";
import type { Tone } from "@/lib/tone";

export type CourtKey = MatchCourt | Malformed;
export type PhaseKey = MatchPhase | Malformed;

export const COURT_LABEL: Record<CourtKey, string> = {
  red: "赤コート",
  blue: "青コート",
  [MALFORMED]: "コート不明",
};

export const PHASE_LABEL: Record<PhaseKey, string> = {
  setup: "セッティングタイム",
  ready: "試合開始待ち",
  match: "試合中",
  finished: "試合終了",
  [MALFORMED]: "フェーズ不明",
};

export const PHASE_TONE: Record<PhaseKey, Tone> = {
  setup: "warning",
  ready: "info",
  match: "success",
  finished: "neutral",
  [MALFORMED]: "error",
};

export const PHASE_BAND_CLASS: Record<PhaseKey, string> = {
  setup: "border-l-warning",
  ready: "border-l-info",
  match: "border-l-success",
  finished: "border-l-base-300",
  [MALFORMED]: "border-l-error",
};

export const COURT_TONE: Record<CourtKey, Tone> = {
  red: "error",
  blue: "info",
  [MALFORMED]: "error",
};

/** 配信されうるコートの三値。`null` は未確定、`MALFORMED` は読めなかった。 */
export type CourtValue = MatchCourt | Malformed | null;

export function courtLabel(court: CourtValue): string {
  return court === null ? "コート未設定" : COURT_LABEL[court];
}

export function courtTone(court: CourtValue): Tone {
  // 未確定は異常ではないので中立色。error にすると壊れていると読める
  return court === null ? "neutral" : COURT_TONE[court];
}

export function isSetupPhase(phase: PhaseKey): boolean {
  return phase === "setup" || phase === "ready";
}

export function isDuringMatch(phase: PhaseKey): boolean {
  return phase === "match";
}
