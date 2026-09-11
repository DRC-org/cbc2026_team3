import { MALFORMED } from "@/lib/protocol";
import type { SwitchDirection, SwitchMeasureSnapshot } from "@/lib/protocol";

export const SWITCH_DIRECTIONS: SwitchDirection[] = [-1, 1];

export type SwitchMeasureOutcome = "idle" | "running" | "failed" | "done";

export interface SwitchMeasureStatus {
  outcome: SwitchMeasureOutcome;
  reasonLabel: string | null;
}

export function switchMeasureStatus(
  state: SwitchMeasureSnapshot,
  connected: boolean,
): SwitchMeasureStatus {
  const reasonLabel = connected ? state.blocked_reason : "切断中のため不可";

  if (state.running) return { outcome: "running", reasonLabel };
  if (state.error !== null || state.result === MALFORMED) return { outcome: "failed", reasonLabel };
  if (state.result !== null) return { outcome: "done", reasonLabel };
  return { outcome: "idle", reasonLabel };
}

export function directionLabel(direction: SwitchDirection): string {
  return direction === 1 ? "+ 方向" : "- 方向";
}

/** 距離測定の状態。作動点測定 (1 本) の実行・結果とは `distances` の有無で見分ける */
export function switchDistanceStatus(
  state: SwitchMeasureSnapshot,
  connected: boolean,
): SwitchMeasureStatus {
  const reasonLabel = connected ? state.blocked_reason : "切断中のため不可";
  const distances = state.distances;

  if (distances === null) return { outcome: "idle", reasonLabel };
  if (state.running) return { outcome: "running", reasonLabel };
  if (state.error !== null || distances === MALFORMED || distances.some((d) => d.error !== null)) {
    return { outcome: "failed", reasonLabel };
  }
  if (distances.length > 0) return { outcome: "done", reasonLabel };
  return { outcome: "idle", reasonLabel };
}
