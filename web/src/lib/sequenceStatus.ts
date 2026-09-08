import type { RobotState, SequenceStepInfo } from "@/lib/protocol";

export type SequenceKind = "no_sequence" | "idle" | "waiting_trigger" | "running" | "complete";

type Progress = Pick<RobotState, "running" | "waiting_trigger" | "step_index" | "total_steps">;

export function isSequenceComplete(state: Progress): boolean {
  return state.total_steps > 0 && state.step_index >= state.total_steps && !state.waiting_trigger;
}

export function sequenceKind(state: Progress): SequenceKind {
  if (state.total_steps === 0) return "no_sequence";
  if (state.waiting_trigger) return "waiting_trigger";
  if (state.running) return "running";
  if (isSequenceComplete(state)) return "complete";
  return "idle";
}

export function isRestartFromTop(state: Progress): boolean {
  return sequenceKind(state) === "idle" && state.step_index > 0;
}

type ProgressWithSteps = Progress & Pick<RobotState, "steps">;

export interface SequenceProgress {
  displayIndex: number;
  total: number;
  percent: number;
  current: SequenceStepInfo | undefined;
}

export function sequenceProgress(state: ProgressWithSteps): SequenceProgress {
  const total = state.total_steps;
  const index = state.step_index;
  const complete = isSequenceComplete(state);

  if (total <= 0) return { displayIndex: 0, total, percent: 0, current: undefined };

  return {
    displayIndex: Math.min(index + 1, total),
    total,
    percent: Math.min(100, (index / total) * 100),
    current: complete ? undefined : state.steps?.[index],
  };
}
