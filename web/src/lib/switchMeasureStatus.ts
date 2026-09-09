import { MALFORMED } from "@/lib/protocol";
import type { SwitchDirection, SwitchMeasureSnapshot } from "@/lib/protocol";

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
