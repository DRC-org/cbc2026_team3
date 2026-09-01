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
 * 準備フェーズ (セッティングタイム)。
 * setup と ready はチェックリストの完了状況で自動遷移する連続した準備期間なので、
 * 画面レイアウトの出し分けでは 1 つのフェーズとして扱う。
 *
 * 読めなかったフェーズはどちらでもない。準備の面へ倒すと、試合中かもしれない
 * 機体に対して指差喚呼とコート設定の画面を出すことになる。
 */
export function isSetupPhase(phase: PhaseKey): boolean {
  return phase === "setup" || phase === "ready";
}

/**
 * 試合中フェーズ。**サーバーのフェーズゲートの写しはここだけに置く。**
 *
 * `lib/match_state.py` の `PHASES_DURING_MATCH` (= {match}) と 1:1 で、
 * 「試合中は不可」のコマンド (`set_param` / `motor_check_start` / `set_court`) が通る
 * `PHASES_OUTSIDE_MATCH` はその補集合なので、この 1 つで両側に答えられる。
 *
 * 可否を決めるのはサーバー (`lib/commands.py`) であって UI ではない。ここは
 * 送る前に理由を説明するためだけに使い、サーバーの判定を組み立て直さないこと。
 * 画面ごとに `phase === "match"` と書き散らすと、フェーズが増えたときに
 * 片方の画面だけが古い条件のまま残る。
 *
 * レイアウトの出し分けに使う `isSetupPhase` とは別物。あちらは finished を
 * 「試合中と同じ情報密度」に寄せるための区分で、コマンドの可否とは一致しない。
 */
export function isDuringMatch(phase: PhaseKey): boolean {
  return phase === "match";
}
