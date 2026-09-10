import { MALFORMED } from "@/lib/protocol";
import type { HomingAxisResult, HomingSnapshot } from "@/lib/protocol";

export type HomingOutcome = "idle" | "running" | "failed" | "done";

export interface HomingStatus {
  outcome: HomingOutcome;
  reasonLabel: string | null;
  failures: HomingAxisResult[] | typeof MALFORMED;
  succeeded: HomingAxisResult[] | typeof MALFORMED;
}

export function homingStatus(state: HomingSnapshot, connected: boolean): HomingStatus {
  const reasonLabel = connected ? state.blocked_reason : "切断中のため不可";
  const results = state.results;
  const failures = results === MALFORMED ? MALFORMED : results.filter((r) => r.error !== null);
  const succeeded = results === MALFORMED ? MALFORMED : results.filter((r) => r.error === null);

  if (state.running) return { outcome: "running", reasonLabel, failures, succeeded };
  if (state.error !== null || failures === MALFORMED || failures.length > 0) {
    return { outcome: "failed", reasonLabel, failures, succeeded };
  }
  if (succeeded !== MALFORMED && succeeded.length > 0) {
    return { outcome: "done", reasonLabel, failures, succeeded };
  }
  return { outcome: "idle", reasonLabel, failures, succeeded };
}

export function robotTargets(
  state: HomingSnapshot,
  robot?: string,
): [string, string[]][] | typeof MALFORMED {
  if (state.targets === MALFORMED) return MALFORMED;
  const entries = Object.entries(state.targets);
  return robot === undefined ? entries : entries.filter(([name]) => name === robot);
}
