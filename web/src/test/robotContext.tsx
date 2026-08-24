import { render } from "@testing-library/react";
import type { ComponentProps, ReactElement } from "react";
import { vi } from "vitest";

import { RobotProvider } from "@/context/RobotContext";
import type { MatchState, MotorCheckState } from "@/hooks/useRobotSocket";

// RobotContextValue は非公開なので、Provider の props から復元する
export type RobotContextValue = ComponentProps<typeof RobotProvider>["value"];

export const EMPTY_MOTOR_CHECK: MotorCheckState = {
  status: "idle",
  current: null,
  progress: null,
  records: [],
  snapshot: null,
  error: null,
  startedAt: null,
  finishedAt: null,
};

export const DEFAULT_MATCH_STATE: MatchState = {
  mode: "semi_auto",
  court: "red",
  phase: "setup",
  required_roles: ["main_hand", "sub_hand"],
  can_start_match: false,
  checklists: {},
};

/** 全ハンドラを vi.fn() にした既定値。テストは関心のあるフィールドだけ上書きする */
export function createRobotContext(overrides: Partial<RobotContextValue> = {}): RobotContextValue {
  return {
    states: {},
    connected: true,
    eStopActive: false,
    healthEvents: [],
    motorChecks: {},
    matchState: DEFAULT_MATCH_STATE,
    rejection: null,
    clearRejection: vi.fn(),
    send: vi.fn(),
    onEStop: vi.fn(),
    onEStopRelease: vi.fn(),
    setMode: vi.fn(),
    setCourt: vi.fn(),
    setChecklistItem: vi.fn(),
    resetChecklist: vi.fn(),
    matchStart: vi.fn(),
    matchFinish: vi.fn(),
    matchReset: vi.fn(),
    ...overrides,
  };
}

interface RenderWithRobotResult extends ReturnType<typeof render> {
  context: RobotContextValue;
}

export function renderWithRobot(
  ui: ReactElement,
  overrides: Partial<RobotContextValue> = {},
): RenderWithRobotResult {
  const context = createRobotContext(overrides);
  const result = render(<RobotProvider value={context}>{ui}</RobotProvider>);
  return { ...result, context };
}
