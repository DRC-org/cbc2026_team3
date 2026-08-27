import type { MatchCourt, MatchPhase } from "@/lib/protocol";
import type { Tone } from "@/lib/tone";

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
export function isDuringMatch(phase: MatchPhase): boolean {
  return phase === "match";
}
