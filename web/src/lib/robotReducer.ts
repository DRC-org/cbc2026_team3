import type { LinkState } from "@/lib/linkQuality";
import { INITIAL_LINK_STATE } from "@/lib/linkQuality";
import type {
  HealthChange,
  HomingSnapshot,
  MatchState,
  MotorCheckSnapshot,
  RobotState,
  ReturnHomeSnapshot,
  ServerInfo,
  ServerMessage,
  SwitchMeasureSnapshot,
} from "@/lib/protocol";
import { mergeRobotState } from "@/lib/robotState";
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
  returnHome: ReturnHomeSnapshot;
  switchMeasure: SwitchMeasureSnapshot;
  matchState: MatchState;
  serverInfo: ServerInfo;
  rejection: CommandRejectedEvent | null;
  link: LinkState;
}

export type RobotAction =
  | { type: "message"; message: ServerMessage; nowMs: EpochMs }
  | { type: "e_stop_local"; active: boolean }
  | { type: "command_unsent"; command: string; reason: string; nowMs: EpochMs }
  | { type: "ping_sent"; nowMs: EpochMs }
  | { type: "link_reset" }
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
    running: false,
    targets: {},
    robots: {},
  };
}

export function emptyReturnHomeState(): ReturnHomeSnapshot {
  return {
    available: false,
    running: false,
    robots: {},
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
    distances: null,
    error: null,
    targets: {},
  };
}

const INITIAL_MATCH_STATE: MatchState = {
  court: null,
  phase: "setup",
  can_start_match: false,
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
  returnHome: emptyReturnHomeState(),
  switchMeasure: emptySwitchMeasureState(),
  matchState: INITIAL_MATCH_STATE,
  serverInfo: INITIAL_SERVER_INFO,
  rejection: null,
  link: INITIAL_LINK_STATE,
};

const HEALTH_EVENT_BUFFER = 5;

function applyMessage(state: RobotUiState, message: ServerMessage, nowMs: EpochMs): RobotUiState {
  switch (message.type) {
    case "state": {
      const previous = state.states[message.robot];
      // 差分だけを先に受けても組み立てられない。全欄の 1 通が来るまで待つ
      if (!message.full && previous === undefined) return state;
      const robotState =
        message.full || previous === undefined
          ? message.state
          : mergeRobotState(previous, message.state);

      const next: RobotUiState = {
        ...state,
        states: { ...state.states, [message.robot]: robotState },
      };
      if (typeof robotState.e_stop_active !== "boolean") return next;
      next.eStopActive = robotState.e_stop_active;
      if (!robotState.e_stop_active) next.eStopReason = null;
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

    case "return_home_state":
      return { ...state, returnHome: message.returnHome };

    case "switch_measure_state":
      return { ...state, switchMeasure: message.switchMeasure };

    case "pong": {
      // 目印を返せないサーバー (古い版) では往復時間を出さない。0 で埋めると速く見える
      if (message.t === null) return state;
      return {
        ...state,
        link: { ...state.link, rttMs: Math.max(0, nowMs - message.t), lastPongAtMs: nowMs },
      };
    }
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
    case "ping_sent":
      return { ...state, link: { ...state.link, lastPingAtMs: action.nowMs } };
    // 繋ぎ直したら測り直す (前の回線の往復時間を出し続けない)
    case "link_reset":
      return state.link === INITIAL_LINK_STATE ? state : { ...state, link: INITIAL_LINK_STATE };
    case "clear_rejection":
      return state.rejection === null ? state : { ...state, rejection: null };
  }
}
