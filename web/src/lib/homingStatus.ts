import { MALFORMED } from "@/lib/protocol";
import type { HomingAxisResult, HomingRobotSnapshot, HomingSnapshot } from "@/lib/protocol";

export type HomingOutcome = "idle" | "running" | "failed" | "done";

export interface HomingStatus {
  outcome: HomingOutcome;
  reasonLabel: string | null;
  failures: HomingAxisResult[] | typeof MALFORMED;
  succeeded: HomingAxisResult[] | typeof MALFORMED;
}

/** undefined は未受信か、そのロボットの零点合わせが配られていない */
export type HomingEntry = HomingRobotSnapshot | typeof MALFORMED | undefined;

const DISCONNECTED = "切断中のため不可";

export function homingEntry(state: HomingSnapshot, robot: string): HomingEntry {
  if (state.robots === MALFORMED) return MALFORMED;
  return Object.hasOwn(state.robots, robot) ? state.robots[robot] : undefined;
}

export function homingStatus(entry: HomingEntry, connected: boolean): HomingStatus {
  if (entry === MALFORMED) {
    return {
      outcome: "failed",
      reasonLabel: connected
        ? "零点合わせの状態を読み取れませんでした (配信の形が読めていません)"
        : DISCONNECTED,
      failures: MALFORMED,
      succeeded: MALFORMED,
    };
  }
  if (entry === undefined) {
    return {
      outcome: "idle",
      reasonLabel: connected ? "サーバーから零点合わせの状態を受信していません" : DISCONNECTED,
      failures: [],
      succeeded: [],
    };
  }

  const reasonLabel = connected ? entry.blocked_reason : DISCONNECTED;
  const results = entry.results;
  const failures = results === MALFORMED ? MALFORMED : results.filter((r) => r.error !== null);
  const succeeded = results === MALFORMED ? MALFORMED : results.filter((r) => r.error === null);

  if (entry.running) return { outcome: "running", reasonLabel, failures, succeeded };
  if (entry.error !== null || failures === MALFORMED || failures.length > 0) {
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
