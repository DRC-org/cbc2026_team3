import type { ManualAxis } from "@/lib/protocol";
import type { Tone } from "@/lib/tone";

export interface SyncVerdict {
  tone: Tone;
  ratio: number | null;
  alert: boolean;
}

export const SYNC_WARN_RATIO = 0.6;

export function evaluateSync(axis: Pick<ManualAxis, "deviation" | "sync_tolerance">): SyncVerdict {
  const { deviation, sync_tolerance: tolerance } = axis;

  if (typeof deviation !== "number" || !Number.isFinite(deviation)) {
    return { tone: "neutral", ratio: null, alert: false };
  }
  if (typeof tolerance !== "number" || !Number.isFinite(tolerance) || tolerance <= 0) {
    return { tone: "neutral", ratio: null, alert: false };
  }

  const ratio = Math.abs(deviation) / tolerance;
  if (ratio > 1) return { tone: "error", ratio, alert: true };
  if (ratio >= SYNC_WARN_RATIO) return { tone: "warning", ratio, alert: true };
  return { tone: "success", ratio, alert: false };
}
