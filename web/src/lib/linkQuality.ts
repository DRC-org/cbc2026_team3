import type { EpochMs } from "@/lib/time";
import type { Tone } from "@/lib/tone";

export interface LinkState {
  /** 直近に測れた往復時間 (ms)。まだ測れていなければ null */
  rttMs: number | null;
  /** 最後に ping を送った時刻。無応答の判定はこれを「今」の代わりに使う */
  lastPingAtMs: EpochMs | null;
  /** 最後に pong が返った時刻 */
  lastPongAtMs: EpochMs | null;
}

export const INITIAL_LINK_STATE: LinkState = {
  rttMs: null,
  lastPingAtMs: null,
  lastPongAtMs: null,
};

/** 往復がこの時間を超えたら「遅い」。手動のジョグが目に見えて遅れ始める辺り */
export const LINK_SLOW_MS = 200;
/** ここを超えると操作が成立しない (ジョグを止めた指令も同じだけ遅れる) */
export const LINK_BAD_MS = 600;
/** ping を送ってこれだけ返らなければ、値としての往復時間は出さず「無応答」を出す */
export const LINK_SILENT_MS = 3000;

export interface LinkVerdict {
  tone: Tone;
  label: string;
}

/**
 * 回線の速さを 1 行にする。
 *
 * **「繋がっている」と「操作が届く」は別物** —— WebSocket は開いたままでも、細い WiFi では
 * 指令が数秒遅れて届く。その遅れを操縦者が読めるようにするための唯一の表示元。
 */
export function linkVerdict(link: LinkState, connected: boolean): LinkVerdict {
  if (!connected) return { tone: "error", label: "Disconnected" };

  const silent =
    link.lastPingAtMs !== null && link.lastPingAtMs - (link.lastPongAtMs ?? 0) >= LINK_SILENT_MS;
  if (silent) return { tone: "error", label: "応答なし" };

  if (link.rttMs === null) return { tone: "success", label: "Connected" };
  const label = `${Math.round(link.rttMs)}ms`;
  if (link.rttMs >= LINK_BAD_MS) return { tone: "error", label };
  if (link.rttMs >= LINK_SLOW_MS) return { tone: "warning", label };
  return { tone: "success", label };
}
