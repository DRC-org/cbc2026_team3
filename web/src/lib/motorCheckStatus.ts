import type { MotorCheckSnapshot } from "@/lib/protocol";

export type MotorCheckOutcome = "idle" | "running" | "failed" | "done";

export interface MotorCheckStatus {
  outcome: MotorCheckOutcome;
  completedSteps: number;
  reasonLabel: string | null;
  failureReason: string | null;
}

function isComplete(state: MotorCheckSnapshot): boolean {
  return state.total_steps > 0 && state.step_index >= state.total_steps;
}

export function motorCheckStatus(state: MotorCheckSnapshot, connected: boolean): MotorCheckStatus {
  const reasonLabel = connected ? state.blocked_reason : "切断中のため不可";
  const failureReason = state.error ?? state.last_error?.message ?? null;

  if (state.running) {
    return { outcome: "running", completedSteps: state.step_index, reasonLabel, failureReason };
  }
  if (failureReason) {
    return { outcome: "failed", completedSteps: state.step_index, reasonLabel, failureReason };
  }
  if (isComplete(state)) {
    return { outcome: "done", completedSteps: state.total_steps, reasonLabel, failureReason };
  }
  return { outcome: "idle", completedSteps: 0, reasonLabel, failureReason };
}
