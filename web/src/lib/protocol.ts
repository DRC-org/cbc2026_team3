import type { EpochSeconds } from "@/lib/time";

export const MALFORMED = "malformed";
export type Malformed = typeof MALFORMED;

export type Measured = number | null;

export function readMeasured(value: unknown): Measured | Malformed {
  if (value === null) return null;
  return typeof value === "number" && Number.isFinite(value) ? value : MALFORMED;
}

export function readCommand(value: unknown): Measured | Malformed {
  return value === undefined ? null : readMeasured(value);
}

export interface MotorState {
  pos: Measured;
  vel: Measured;
  torque: Measured;
  temp: Measured;
  command: Measured;
  command_mode: string | null;
}

export const BUS_HEALTH_STATES = ["ok", "degraded", "down"] as const;
export type BusHealthState = (typeof BUS_HEALTH_STATES)[number];

export const MOTOR_HEALTH_STATES = ["ok", "stale", "warning", "fault"] as const;
export type MotorHealthState = (typeof MOTOR_HEALTH_STATES)[number];

export interface BusHealth {
  name: string;
  channel: string;
  state: BusHealthState;
  last_tx_at: EpochSeconds | null;
  last_rx_at: EpochSeconds | null;
  tx_error_count: number;
  rx_error_count: number;
  bus_off: boolean;
  rx_down: boolean;
  rx_down_episodes: number;
  may_affect_workpiece: boolean;
}

export interface MotorHealth {
  name: string;
  bus: string;
  state: MotorHealthState;
  last_feedback_at: EpochSeconds | null;
  feedback_age_ms: number | null;
  temperature: number | null;
  detail: string | null;
}

export interface HealthSnapshot {
  timestamp: EpochSeconds;
  overall: BusHealthState;
  buses: BusHealth[];
  motors: MotorHealth[];
  detail: string | null;
}

function isHealthEntryArray(value: unknown, states: readonly string[]): boolean {
  return (
    Array.isArray(value) &&
    value.every(
      (entry) =>
        isObject(entry) &&
        typeof entry.name === "string" &&
        typeof entry.state === "string" &&
        states.includes(entry.state),
    )
  );
}

export function healthShapeErrors(value: unknown): string[] {
  if (!isObject(value)) return ["health"];

  const broken: string[] = [];
  if (typeof value.overall !== "string" || !BUS_HEALTH_STATES.includes(value.overall as never)) {
    broken.push("overall");
  }
  if (!isHealthEntryArray(value.buses, BUS_HEALTH_STATES)) broken.push("buses");
  if (!isHealthEntryArray(value.motors, MOTOR_HEALTH_STATES)) broken.push("motors");
  if (value.detail !== null && value.detail !== undefined && typeof value.detail !== "string") {
    broken.push("detail");
  }
  return broken;
}

export function parseHealth(raw: unknown): HealthSnapshot | Malformed | undefined {
  if (raw === undefined) return undefined;
  return healthShapeErrors(raw).length === 0 ? (raw as HealthSnapshot) : MALFORMED;
}

export type HealthChangeLevel = "info" | "warning" | "critical";

export interface HealthChange {
  robot: string;
  level: HealthChangeLevel;
  target: string;
  from: string;
  to: string;
  message: string;
}

export interface MotorCheckSnapshot {
  available: boolean;
  blocked_reason: string | null;
  running: boolean;
  current_step: string | null;
  step_index: number;
  total_steps: number;
  steps: SequenceStepInfo[] | Malformed;
  error: string | null;
  last_error: SequenceFailure | null;
  excluded_steps: ExcludedStep[] | Malformed;
}

export interface ServerInfo {
  dev_tools: boolean;
  dry_run: boolean;
  temp_warning_c: number | null;
  temp_critical_c: number | null;
}

export const MATCH_COURTS = ["red", "blue"] as const;
export type MatchCourt = (typeof MATCH_COURTS)[number];

export const MATCH_PHASES = ["setup", "ready", "match", "finished"] as const;
export type MatchPhase = (typeof MATCH_PHASES)[number];
export type ChecklistRole = "pre_match";

export const CHECKLIST_ROLE: ChecklistRole = "pre_match";

export interface ChecklistItem {
  id: string;
  label: string;
  checked: boolean;
  group?: string | null;
}

export interface ChecklistState {
  items: ChecklistItem[];
  completed: boolean;
}

function isChecklistState(value: unknown): boolean {
  if (!isObject(value)) return false;
  if (typeof value.completed !== "boolean") return false;
  return (
    Array.isArray(value.items) &&
    value.items.every(
      (item) =>
        isObject(item) &&
        typeof item.id === "string" &&
        typeof item.label === "string" &&
        typeof item.checked === "boolean" &&
        (item.group === undefined || item.group === null || typeof item.group === "string"),
    )
  );
}

export function parseChecklists(raw: unknown): Record<string, ChecklistState> | Malformed {
  if (raw === undefined) return {};
  if (!isObject(raw)) return MALFORMED;
  if (!Object.values(raw).every(isChecklistState)) return MALFORMED;
  return raw as Record<string, ChecklistState>;
}

export interface MatchTimer {
  running: boolean;
  elapsed_ms: number;
  duration_ms: number;
}

export interface MatchState {
  court: MatchCourt | Malformed;
  phase: MatchPhase | Malformed;
  can_start_match: boolean;
  checklists: Record<string, ChecklistState> | Malformed;
  timer: MatchTimer | null;
}

export interface SequenceStepInfo {
  index: number;
  label: string;
  require_trigger: boolean;
}

export interface SequenceFailure {
  step_index: number;
  step: string;
  message: string;
}

export function parseSequenceFailure(raw: unknown): SequenceFailure | null {
  if (!isObject(raw)) return null;
  if (typeof raw.step_index !== "number" || !Number.isFinite(raw.step_index)) return null;
  if (typeof raw.step !== "string") return null;
  if (typeof raw.message !== "string" || raw.message.length === 0) return null;
  return { step_index: raw.step_index, step: raw.step, message: raw.message };
}

export interface ExcludedStep {
  step: string;
  missing_axes: string[];
}

export function parseExcludedSteps(raw: unknown): ExcludedStep[] | Malformed {
  if (!Array.isArray(raw)) return MALFORMED;
  const ok = raw.every(
    (item) =>
      isObject(item) &&
      typeof item.step === "string" &&
      Array.isArray(item.missing_axes) &&
      item.missing_axes.every((axis) => typeof axis === "string"),
  );
  return ok ? (raw as ExcludedStep[]) : MALFORMED;
}

export function parseMotorCheckSteps(raw: unknown): SequenceStepInfo[] | Malformed {
  if (!Array.isArray(raw)) return MALFORMED;
  const ok = raw.every(
    (item) =>
      isObject(item) &&
      typeof item.index === "number" &&
      typeof item.label === "string" &&
      typeof item.require_trigger === "boolean",
  );
  return ok ? (raw as SequenceStepInfo[]) : MALFORMED;
}

export interface PositionLoopState {
  bus: string;
  running: boolean;
  paused: boolean;
  sync_violations: string[];
}

export interface SyncMonitorState {
  axes: string[];
  running: boolean;
  violated: string[];
}

export interface TargetRefresherState {
  motors: string[];
  running: boolean;
  paused: boolean;
}

export interface SafetyState {
  sync_violations: string[];
  unenergized_motors: string[];
  firmware_unconfirmed_motors: string[];
  limit_latched: Record<string, string[]>;
  limit_blind_sensors: string[];
  failed_tasks: string[];
  reenergizing: boolean;
  loops_running: boolean;
  monitors_running: boolean;
  refreshers_running: boolean;
  position_loops: PositionLoopState[];
  sync_monitors: SyncMonitorState[];
  target_refreshers: TargetRefresherState[];
}

function isStringArray(value: unknown): boolean {
  return Array.isArray(value) && value.every((item) => typeof item === "string");
}

function isStringArrayRecord(value: unknown): boolean {
  return isObject(value) && Object.values(value).every(isStringArray);
}

const SAFETY_TASK_SHAPES: Record<string, (task: Raw) => boolean> = {
  position_loops: (t) => typeof t.bus === "string" && typeof t.running === "boolean",
  sync_monitors: (t) => isStringArray(t.axes) && typeof t.running === "boolean",
  target_refreshers: (t) => isStringArray(t.motors) && typeof t.running === "boolean",
};

export function safetyShapeErrors(value: unknown): string[] {
  if (!isObject(value)) return ["safety"];

  const broken: string[] = [];
  for (const key of [
    "sync_violations",
    "unenergized_motors",
    "firmware_unconfirmed_motors",
    "limit_blind_sensors",
    "failed_tasks",
  ]) {
    if (!isStringArray(value[key])) broken.push(key);
  }
  if (!isStringArrayRecord(value.limit_latched)) broken.push("limit_latched");
  for (const key of ["loops_running", "monitors_running", "refreshers_running", "reenergizing"]) {
    if (typeof value[key] !== "boolean") broken.push(key);
  }
  for (const [key, isValidTask] of Object.entries(SAFETY_TASK_SHAPES)) {
    const tasks = value[key];
    if (!Array.isArray(tasks) || !tasks.every((t) => isObject(t) && isValidTask(t))) {
      broken.push(key);
    }
  }
  return broken;
}

export function parseSafety(raw: unknown): SafetyState | Malformed | undefined {
  if (raw === undefined) return undefined;
  return safetyShapeErrors(raw).length === 0 ? (raw as SafetyState) : MALFORMED;
}

export type OperationMode = "sequence" | "manual";

export interface ManualRange {
  min: number;
  max: number;
  steps: number[];
}

export interface ManualAxis {
  name: string;
  unit: string;
  command_mode: "position" | "velocity" | "current" | "duty" | "on_off";
  value: number | null;
  target: number | null;
  manual: ManualRange | null;
  manual_always: boolean;
  deviation: number | null;
  sync_tolerance: number | null;
  positions: ManualPosition[];
  motors: string[];
}

export interface ManualPosition {
  name: string;
  value: number | null;
}

export interface ManualState {
  mode: OperationMode;
  axes: ManualAxis[];
}

export interface SensorState {
  active: boolean | null;
  stale: boolean;
}

function isSensorState(value: unknown): boolean {
  if (!isObject(value)) return false;
  if (typeof value.stale !== "boolean") return false;
  return value.active === null || typeof value.active === "boolean";
}

export function parseSensors(raw: unknown): Record<string, SensorState> | Malformed | undefined {
  if (raw === undefined) return undefined;
  if (!isObject(raw)) return MALFORMED;
  if (!Object.values(raw).every(isSensorState)) return MALFORMED;
  return raw as Record<string, SensorState>;
}

function parseManualPosition(raw: unknown): ManualPosition | null {
  if (typeof raw === "string") return { name: raw, value: null };
  if (!isObject(raw)) return null;
  if (typeof raw.name !== "string") return null;
  if (raw.value !== null && typeof raw.value !== "number") return null;
  return { name: raw.name, value: raw.value as number | null };
}

function parseManual(raw: unknown): ManualState | undefined {
  if (!isObject(raw)) return undefined;
  if (!Array.isArray(raw.axes)) return raw as unknown as ManualState;
  const axes = raw.axes.map((axis: unknown) => {
    if (!isObject(axis) || !Array.isArray(axis.positions)) return axis;
    return {
      ...axis,
      positions: axis.positions.map(parseManualPosition).filter((p) => p !== null),
    };
  });
  return { ...raw, axes } as unknown as ManualState;
}

export interface RobotState {
  type?: "state";
  robot: string;
  sequence: string;
  current_step: string | null;
  step_index: number;
  total_steps: number;
  waiting_trigger: boolean;
  running?: boolean;
  motors: Record<string, MotorState>;
  sensors?: Record<string, SensorState> | Malformed;
  e_stop_active?: boolean;
  health?: HealthSnapshot | Malformed;
  last_error?: SequenceFailure | null;
  safety?: SafetyState | Malformed;
  steps?: SequenceStepInfo[];
  manual?: ManualState;
}

export type ServerMessage =
  | { type: "state"; robot: string; state: RobotState }
  | { type: "server_info"; serverInfo: ServerInfo }
  | { type: "match_state"; matchState: MatchState }
  | { type: "e_stop_state"; active: boolean; reason: string | null }
  | { type: "command_rejected"; command: string; reason: string }
  | { type: "health_change"; event: HealthChange }
  | { type: "motor_check_state"; motorCheck: MotorCheckSnapshot };

type Raw = Record<string, unknown>;

function str(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function num(value: unknown, fallback = 0): number {
  return typeof value === "number" ? value : fallback;
}

function isObject(value: unknown): value is Raw {
  return typeof value === "object" && value !== null;
}

function parseTimer(raw: unknown): MatchTimer | null {
  if (!isObject(raw)) return null;
  if (typeof raw.running !== "boolean") return null;
  if (typeof raw.elapsed_ms !== "number" || !Number.isFinite(raw.elapsed_ms)) return null;
  if (typeof raw.duration_ms !== "number" || !Number.isFinite(raw.duration_ms)) return null;
  if (raw.duration_ms <= 0) return null;
  return { running: raw.running, elapsed_ms: raw.elapsed_ms, duration_ms: raw.duration_ms };
}

function parseEnum<T extends string>(raw: unknown, allowed: readonly T[]): T | Malformed {
  return typeof raw === "string" && (allowed as readonly string[]).includes(raw)
    ? (raw as T)
    : MALFORMED;
}

const HEALTH_CHANGE_LEVELS: readonly HealthChangeLevel[] = ["info", "warning", "critical"];

function parseHealthChangeLevel(raw: unknown): HealthChangeLevel {
  return typeof raw === "string" && (HEALTH_CHANGE_LEVELS as readonly string[]).includes(raw)
    ? (raw as HealthChangeLevel)
    : "critical";
}

function robotOf(raw: Raw): string | null {
  return typeof raw.robot === "string" && raw.robot.length > 0 ? raw.robot : null;
}

function parseKnown(raw: Raw): ServerMessage | null {
  const robot = robotOf(raw);

  switch (raw.type) {
    case "state": {
      if (robot === null) return null;
      const state = { ...(raw as unknown as RobotState) };
      const safety = parseSafety(raw.safety);
      if (safety !== undefined) state.safety = safety;
      const health = parseHealth(raw.health);
      if (health !== undefined) state.health = health;
      const sensors = parseSensors(raw.sensors);
      if (sensors !== undefined) state.sensors = sensors;
      state.last_error = parseSequenceFailure(raw.last_error);
      if (raw.manual !== undefined) state.manual = parseManual(raw.manual);

      return { type: "state", robot, state };
    }

    case "server_info":
      return {
        type: "server_info",
        serverInfo: {
          dev_tools: raw.dev_tools === true,
          dry_run: raw.dry_run === true,
          temp_warning_c: typeof raw.temp_warning_c === "number" ? raw.temp_warning_c : null,
          temp_critical_c: typeof raw.temp_critical_c === "number" ? raw.temp_critical_c : null,
        },
      };

    case "match_state":
      return {
        type: "match_state",
        matchState: {
          court: parseEnum(raw.court, MATCH_COURTS),
          phase: parseEnum(raw.phase, MATCH_PHASES),
          can_start_match: Boolean(raw.can_start_match),
          checklists: parseChecklists(raw.checklists),
          timer: parseTimer(raw.timer),
        },
      };

    case "e_stop_state":
      if (typeof raw.active !== "boolean") return null;
      return {
        type: "e_stop_state",
        active: raw.active,
        reason: raw.active && typeof raw.reason === "string" ? raw.reason : null,
      };

    case "command_rejected":
      return { type: "command_rejected", command: str(raw.command), reason: str(raw.reason) };

    case "health_change":
      if (robot === null) return null;
      return {
        type: "health_change",
        event: {
          robot,
          level: parseHealthChangeLevel(raw.level),
          target: str(raw.target),
          from: str(raw.from),
          to: str(raw.to),
          message: str(raw.message),
        },
      };

    case "motor_check_state":
      return {
        type: "motor_check_state",
        motorCheck: {
          available: raw.available === true,
          blocked_reason: typeof raw.blocked_reason === "string" ? raw.blocked_reason : null,
          running: raw.running === true,
          current_step: typeof raw.current_step === "string" ? raw.current_step : null,
          step_index: num(raw.step_index),
          total_steps: num(raw.total_steps),
          steps: parseMotorCheckSteps(raw.steps),
          error: typeof raw.error === "string" ? raw.error : null,
          last_error: parseSequenceFailure(raw.last_error),
          excluded_steps: parseExcludedSteps(raw.excluded_steps),
        },
      };

    default:
      return null;
  }
}

export function parseServerMessage(data: string): ServerMessage | null {
  let raw: unknown;
  try {
    raw = JSON.parse(data);
  } catch {
    return null;
  }
  return isObject(raw) ? parseKnown(raw) : null;
}
