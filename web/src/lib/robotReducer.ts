import type {
  HealthChange,
  HomingSnapshot,
  MatchState,
  MotorCheckSnapshot,
  RobotState,
  ServerInfo,
  ServerMessage,
  SwitchMeasureSnapshot,
} from "@/lib/protocol";
import type { EpochMs } from "@/lib/time";

export interface HealthChangeEvent extends HealthChange {
  receivedAtMs: EpochMs;
}

export interface CommandRejectedEvent {
  command: string;
  reason: string;
  receivedAtMs: EpochMs;
  source: "server" | "local";
}

export interface RobotUiState {
  states: Record<string, RobotState>;
  eStopActive: boolean;
  eStopReason: string | null;
  healthEvents: HealthChangeEvent[];
  motorCheck: MotorCheckSnapshot;
  homing: HomingSnapshot;
  switchMeasure: SwitchMeasureSnapshot;
  matchState: MatchState;
  serverInfo: ServerInfo;
  rejection: CommandRejectedEvent | null;
}

export type RobotAction =
  | { type: "message"; message: ServerMessage; nowMs: EpochMs }
  | { type: "e_stop_local"; active: boolean }
  | { type: "command_unsent"; command: string; reason: string; nowMs: EpochMs }
  | { type: "clear_rejection" };

export function emptyMotorCheckState(): MotorCheckSnapshot {
  return {
    available: false,
    blocked_reason: "サーバーから動作確認の状態を受信していません",
    running: false,
    current_step: null,
    step_index: 0,
    total_steps: 0,
    steps: [],
    error: null,
    last_error: null,
    excluded_steps: [],
  };
}

export function emptyHomingState(): HomingSnapshot {
  return {
    available: false,
    blocked_reason: "サーバーから零点合わせの状態を受信していません",
    running: false,
    robot: null,
    axes: [],
    current_axis: null,
    results: [],
    error: null,
    targets: {},
  };
}

export function emptySwitchMeasureState(): SwitchMeasureSnapshot {
  return {
    available: false,
    blocked_reason: "サーバーから作動点測定の状態を受信していません",
    running: false,
    robot: null,
    axis: null,
    direction: null,
    result: null,
    error: null,
    targets: {},
  };
}

const INITIAL_MATCH_STATE: MatchState = {
  court: null,
  phase: "setup",
  can_start_match: false,
  checklists: {},
  timer: null,
};

const INITIAL_SERVER_INFO: ServerInfo = {
  dev_tools: false,
  dry_run: false,
  temp_warning_c: null,
  temp_critical_c: null,
};

export const INITIAL_ROBOT_UI_STATE: RobotUiState = {
  states: {},
  eStopActive: false,
  eStopReason: null,
  healthEvents: [],
  motorCheck: emptyMotorCheckState(),
  homing: emptyHomingState(),
  switchMeasure: emptySwitchMeasureState(),
  matchState: INITIAL_MATCH_STATE,
  serverInfo: INITIAL_SERVER_INFO,
  rejection: null,
};

const HEALTH_EVENT_BUFFER = 5;

function applyMessage(state: RobotUiState, message: ServerMessage, nowMs: EpochMs): RobotUiState {
  switch (message.type) {
    case "state": {
      const next: RobotUiState = {
        ...state,
        states: { ...state.states, [message.robot]: message.state },
      };
      if (typeof message.state.e_stop_active !== "boolean") return next;
      next.eStopActive = message.state.e_stop_active;
      if (!message.state.e_stop_active) next.eStopReason = null;
      return next;
    }

    case "server_info":
      return { ...state, serverInfo: message.serverInfo };

    case "match_state":
      return { ...state, matchState: message.matchState };

    case "e_stop_state":
      return state.eStopActive === message.active && state.eStopReason === message.reason
        ? state
        : { ...state, eStopActive: message.active, eStopReason: message.reason };

    case "command_rejected":
      return {
        ...state,
        rejection: {
          command: message.command,
          reason: message.reason,
          receivedAtMs: nowMs,
          source: "server",
        },
      };

    case "health_change": {
      const next = [{ ...message.event, receivedAtMs: nowMs }, ...state.healthEvents];
      return {
        ...state,
        healthEvents: next.length > HEALTH_EVENT_BUFFER ? next.slice(0, HEALTH_EVENT_BUFFER) : next,
      };
    }

    case "motor_check_state":
      return { ...state, motorCheck: message.motorCheck };

    case "homing_state":
      return { ...state, homing: message.homing };

    case "switch_measure_state":
      return { ...state, switchMeasure: message.switchMeasure };
  }
}

export function robotReducer(state: RobotUiState, action: RobotAction): RobotUiState {
  switch (action.type) {
    case "message":
      return applyMessage(state, action.message, action.nowMs);
    case "e_stop_local":
      return state.eStopActive === action.active ? state : { ...state, eStopActive: action.active };
    case "command_unsent":
      return {
        ...state,
        rejection: {
          command: action.command,
          reason: action.reason,
          receivedAtMs: action.nowMs,
          source: "local",
        },
      };
    case "clear_rejection":
      return state.rejection === null ? state : { ...state, rejection: null };
  }
}
