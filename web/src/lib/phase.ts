import { MALFORMED } from "@/lib/protocol";
import type { Malformed, MatchCourt, MatchPhase } from "@/lib/protocol";
import type { Tone } from "@/lib/tone";

/**
 * 表示で扱うコート・フェーズ。**読めなかった配信 (`MALFORMED`) を含む。**
 *
 * これらは全て `Record` の索引として使われるので、未知の値が入ると索引が
 * `undefined` になり、`StatusBadge` がクラス無しで描かれてチップが無地・無文字で
 * 消える。「読めなかった」を語彙の 1 つとして持たせ、必ず何かが見えるようにする。
 */
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

/** フェーズの状態色。ヘッダーのチップと左端バーの双方がこれを引く */
export const PHASE_TONE: Record<PhaseKey, Tone> = {
  setup: "warning",
  ready: "info",
  match: "success",
  finished: "neutral",
  // 読めていないことは異常。平常時のどの色とも取り違えられないようにする
  [MALFORMED]: "error",
};

/**
 * ヘッダー帯の左端バー。
 * 帯全面をフェーズ色で塗ると画面で最も明るい面になってしまうため、
 * 地は白のまま固定し、左端のバー色とチップだけでフェーズを示す。
 */
export const PHASE_BAND_CLASS: Record<PhaseKey, string> = {
  setup: "border-l-warning",
  ready: "border-l-info",
  match: "border-l-success",
  finished: "border-l-base-300",
  [MALFORMED]: "border-l-error",
};

/** コートの状態色。赤/青は誤設定のまま試合に入る事故を防ぐため常時表示する */
export const COURT_TONE: Record<CourtKey, Tone> = {
  red: "error",
  blue: "info",
  [MALFORMED]: "error",
};

/**
 * 準備フェーズ (セッティングタイム)。setup と ready は自動遷移する連続した準備期間なので
 * レイアウトの出し分けでは 1 つとして扱う。**読めなかったフェーズはどちらでもない** ——
 * 準備の面へ倒すと、試合中かもしれない機体に指差喚呼とコート設定の画面を出すことになる。
 */
export function isSetupPhase(phase: PhaseKey): boolean {
  return phase === "setup" || phase === "ready";
}

/**
 * 試合中フェーズ。**サーバーのフェーズゲートの写しはここだけに置く**
 * (docs/invariants.md 「フェーズによる可否のサーバー写しは `lib/phase.ts` の
 * `isDuringMatch()` だけ」)。`lib/match_state.py` の `PHASES_DURING_MATCH` と 1:1。
 *
 * レイアウトの出し分けに使う `isSetupPhase` とは別物で、一致させてはならない。
 */
export function isDuringMatch(phase: PhaseKey): boolean {
  return phase === "match";
}
