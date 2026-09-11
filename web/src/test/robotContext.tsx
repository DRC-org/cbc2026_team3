import { render } from "@testing-library/react";
import type { ReactElement } from "react";
import { vi } from "vitest";

import { RobotProvider } from "@/context/RobotContext";
import type { RobotContextValue } from "@/context/RobotContext";
import { INITIAL_LINK_STATE } from "@/lib/linkQuality";
import type {
  HomingSnapshot,
  MatchState,
  MotorCheckSnapshot,
  ServerInfo,
  SwitchMeasureSnapshot,
} from "@/lib/protocol";
import {
  emptyHomingState,
  emptyMotorCheckState,
  emptySwitchMeasureState,
} from "@/lib/robotReducer";

export type { RobotContextValue };

export const EMPTY_MOTOR_CHECK: MotorCheckSnapshot = emptyMotorCheckState();

export const EMPTY_HOMING: HomingSnapshot = emptyHomingState();

export const EMPTY_SWITCH_MEASURE: SwitchMeasureSnapshot = emptySwitchMeasureState();

export const DEFAULT_MATCH_STATE: MatchState = {
  court: "red",
  phase: "setup",
  can_start_match: false,
  timer: null,
};

export const DEFAULT_SERVER_INFO: ServerInfo = {
  dev_tools: false,
  dry_run: false,
  temp_warning_c: null,
  temp_critical_c: null,
};

export function createRobotContext(overrides: Partial<RobotContextValue> = {}): RobotContextValue {
  return {
    states: {},
    connected: true,
    eStopActive: false,
    eStopReason: null,
    eStopOverlayHidden: false,
    link: INITIAL_LINK_STATE,
    healthEvents: [],
    motorCheck: emptyMotorCheckState(),
    homing: emptyHomingState(),
    switchMeasure: emptySwitchMeasureState(),
    matchState: DEFAULT_MATCH_STATE,
    serverInfo: DEFAULT_SERVER_INFO,
    rejection: null,
    clearRejection: vi.fn(),
    wsUrl: "ws://localhost:8080/ws",
    wsUrlSource: "origin",
    setWsUrl: vi.fn(() => true),
    resetWsUrl: vi.fn(),
    openWsSettings: vi.fn(),
    send: vi.fn(() => true),
    sendOrReport: vi.fn(() => true),
    onEStop: vi.fn(),
    onEStopRelease: vi.fn(),
    hideEStopOverlay: vi.fn(),
    setCourt: vi.fn(),
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
