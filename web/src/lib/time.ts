export type EpochSeconds = number;

export type EpochMs = number;

export function formatClock(ms: EpochMs | null | undefined): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return "—";
  return new Date(ms).toLocaleTimeString("ja-JP", { hour12: false });
}

export function formatAge(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || Number.isNaN(ms)) return "—";
  if (ms < 0) return "—";
  if (ms < 1000) return `${Math.round(ms)}ms 前`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s 前`;
  if (ms < 3_600_000) return `${Math.floor(ms / 60_000)}m 前`;
  return `${Math.floor(ms / 3_600_000)}h 前`;
}
