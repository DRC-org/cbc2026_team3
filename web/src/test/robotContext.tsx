import { render } from "@testing-library/react";
import type { ReactElement } from "react";
import { vi } from "vitest";

import { RobotProvider } from "@/context/RobotContext";
import type { RobotContextValue } from "@/context/RobotContext";
import type { MatchState } from "@/lib/protocol";
import type { MotorCheckState } from "@/lib/robotReducer";
import { emptyMotorCheckState } from "@/lib/robotReducer";

export type { RobotContextValue };

export const EMPTY_MOTOR_CHECK: MotorCheckState = emptyMotorCheckState();

export const DEFAULT_MATCH_STATE: MatchState = {
  court: "red",
  phase: "setup",
  can_start_match: false,
  checklists: {},
  // 既定は「未受信」。走っているタイマーを既定にすると、関心の無いテストが
  // 一律に setTimeout を仕掛けることになる (タイマー自体は MatchTimer.test.tsx が見る)
  timer: null,
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
    send: vi.fn(() => true),
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
