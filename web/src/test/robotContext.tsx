import { render } from "@testing-library/react";
import type { ComponentProps, ReactElement } from "react";
import { vi } from "vitest";

import { RobotProvider } from "@/context/RobotContext";
import type { MatchState, MotorCheckState } from "@/hooks/useRobotSocket";
import { emptyMotorCheckState } from "@/hooks/useRobotSocket";

// RobotContextValue は非公開なので、Provider の props から復元する
export type RobotContextValue = ComponentProps<typeof RobotProvider>["value"];

export const EMPTY_MOTOR_CHECK: MotorCheckState = emptyMotorCheckState();

export const DEFAULT_MATCH_STATE: MatchState = {
  court: "red",
  phase: "setup",
  can_start_match: false,
  checklists: {},
};

/** 全ハンドラを vi.fn() にした既定値。テストは関心のあるフィールドだけ上書きする */
export function createRobotContext(overrides: Partial<RobotContextValue> = {}): RobotContextValue {
  return {
    states: {},
    connected: true,
    eStopActive: false,
    eStopReason: null,
    healthEvents: [],
    motorChecks: {},
    matchState: DEFAULT_MATCH_STATE,
    rejection: null,
    clearRejection: vi.fn(),
    wsUrl: "ws://localhost:8080/ws",
    wsUrlSource: "origin",
    setWsUrl: vi.fn(() => true),
    resetWsUrl: vi.fn(),
    openWsSettings: vi.fn(),
    send: vi.fn(),
    onEStop: vi.fn(),
    onEStopRelease: vi.fn(),
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
