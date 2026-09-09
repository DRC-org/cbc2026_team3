import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useRobotSocket } from "@/hooks/useRobotSocket";
import type {
  BusHealth,
  ChecklistItem,
  ChecklistState,
  ExcludedStep,
  HealthChange,
  HealthSnapshot,
  HomingAxisResult,
  HomingSnapshot,
  LimitMonitorState,
  ManualAxis,
  ManualPosition,
  ManualRange,
  ManualState,
  MatchState,
  MatchTimer,
  MotorCheckSnapshot,
  MotorHealth,
  MotorState,
  PositionLoopState,
  RobotState,
  SafetyState,
  SequenceFailure,
  SensorState,
  SequenceStepInfo,
  ServerInfo,
  ServerMessage,
  SuctionPad,
  SuctionState,
  SwitchMeasureSnapshot,
  SwitchMeasurement,
  SyncMonitorState,
  TargetRefresherState,
} from "@/lib/protocol";
import { installMockWebSocket, latestSocket } from "@/test/mockWebSocket";
import contract from "@/test/ws-contract.json";

const URL = "ws://contract/ws";

type Sample = Record<string, unknown>;

const SAMPLES = contract.samples as unknown as Record<string, Sample>;

type SocketResult = ReturnType<typeof useRobotSocket>;
type Expectation = (result: SocketResult, sample: Sample) => void;

const STATE_FIELDS_UI_READS = [
  "type",
  "robot",
  "sequence",
  "current_step",
  "step_index",
  "total_steps",
  "waiting_trigger",
  "running",
  "steps",
  "motors",
  "sensors",
  "e_stop_active",
  "health",
  "safety",
  "safety.sync_violations",
  "safety.unenergized_motors",
  "safety.firmware_unconfirmed_motors",
  "safety.failed_tasks",
  "safety.reenergizing",
  "safety.loops_running",
  "safety.monitors_running",
  "safety.position_loops",
  "safety.sync_monitors",
  "safety.limit_monitors_running",
  "safety.limit_monitors",
  "safety.refreshers_running",
  "safety.target_refreshers",
  "manual",
  "manual.mode",
  "manual.axes",
  "suction",
  "suction.pads",
] as const;

const EXPECTATIONS: Record<string, Expectation> = {
  state: (result, sample) => {
    const robot = sample.robot as string;
    expect(result.states[robot]).toEqual(sample);
    expect(result.eStopActive).toBe(sample.e_stop_active);
    expect(result.states[robot].running).toBe(sample.running);
    expect(result.states[robot].safety).toEqual(sample.safety);
    expect(result.states[robot].manual).toEqual(sample.manual);
    expect(result.states[robot].sensors).toEqual(sample.sensors);
    expect(result.states[robot].suction).toEqual(sample.suction);
  },

  state_with_last_error: (result, sample) => {
    const robot = sample.robot as string;
    expect(result.states[robot].last_error).toEqual(sample.last_error);
    expect(result.states[robot].last_error?.step.length).toBeGreaterThan(0);
    expect(result.states[robot].last_error?.message.length).toBeGreaterThan(0);
  },

  server_info: (result, sample) => {
    expect(result.serverInfo).toEqual({
      dev_tools: sample.dev_tools,
      dry_run: sample.dry_run,
      temp_warning_c: sample.temp_warning_c,
      temp_critical_c: sample.temp_critical_c,
    });
    expect(typeof result.serverInfo.temp_warning_c).toBe("number");
    expect(typeof result.serverInfo.temp_critical_c).toBe("number");
  },

  match_state: (result, sample) => {
    expect(result.matchState).toEqual({
      court: sample.court,
      phase: sample.phase,
      can_start_match: sample.can_start_match,
      checklists: sample.checklists,
      timer: sample.timer,
    });
  },

  health_change: (result, sample) => {
    expect(result.healthEvents).toHaveLength(1);
    expect(result.healthEvents[0]).toMatchObject({
      robot: sample.robot,
      level: sample.level,
      target: sample.target,
      from: sample.from,
      to: sample.to,
      message: sample.message,
    });
  },

  health_change_bus: (result, sample) => {
    expect(result.healthEvents).toHaveLength(1);
    expect(result.healthEvents[0]).toMatchObject({
      robot: sample.robot,
      target: sample.target,
      level: sample.level,
    });
  },

  e_stop_state: (result, sample) => {
    expect(result.eStopActive).toBe(sample.active);
    expect(result.eStopReason).toBeNull();
  },

  e_stop_state_with_reason: (result, sample) => {
    expect(result.eStopActive).toBe(true);
    expect(result.eStopReason).toBe(sample.reason);
  },

  command_rejected: (result, sample) => {
    expect(result.rejection).toMatchObject({
      command: sample.command,
      reason: sample.reason,
    });
  },

  motor_check_state: (result, sample) => {
    expect(sample.robot).toBeUndefined();

    const state = result.motorCheck;
    expect(state.available).toBe(sample.available);
    expect(state.running).toBe(sample.running);
    expect(state.error).toBe(sample.error);
    expect(state.total_steps).toBe(sample.total_steps);
    expect(state.steps).toEqual(sample.steps);
  },

  homing_state: (result, sample) => {
    const state = result.homing;
    expect(state.available).toBe(sample.available);
    expect(state.running).toBe(sample.running);
    expect(state.robot).toBe(sample.robot);
    expect(state.axes).toEqual(sample.axes);
    expect(state.results).toEqual(sample.results);
    expect(state.targets).toEqual(sample.targets);
    // 失敗した軸の理由が UI まで残る (これが読めないと「なぜ止まったか」が画面から消える)
    expect(state.results).not.toHaveLength(0);
  },

  motor_check_state_with_exclusions: (result, sample) => {
    const state = result.motorCheck;
    expect(state.excluded_steps).toEqual(sample.excluded_steps);
    expect(state.excluded_steps).not.toHaveLength(0);
    expect(state.steps).toEqual(sample.steps);
  },

  switch_measure_state: (result, sample) => {
    const state = result.switchMeasure;
    expect(state.available).toBe(sample.available);
    expect(state.blocked_reason).toBe(sample.blocked_reason);
    expect(state.running).toBe(sample.running);
    expect(state.robot).toBe(sample.robot);
    expect(state.axis).toBe(sample.axis);
    expect(state.direction).toBe(sample.direction);
    expect(state.result).toBeNull();
    expect(state.error).toBeNull();
    expect(state.targets).toEqual(sample.targets);
    expect(Object.keys(state.targets)).not.toHaveLength(0);
  },

  switch_measure_state_with_result: (result, sample) => {
    const state = result.switchMeasure;
    expect(state.robot).toBe(sample.robot);
    expect(state.axis).toBe(sample.axis);
    expect(state.direction).toBe(sample.direction);
    // 実測が数値のまま届く (MALFORMED や 0 埋めに化けると作動点が読めない)
    expect(state.result).toEqual(sample.result);
    expect(state.error).toBeNull();
  },

  switch_measure_state_with_error: (result, sample) => {
    const state = result.switchMeasure;
    expect(state.axis).toBe(sample.axis);
    expect(state.direction).toBe(sample.direction);
    expect(state.result).toBeNull();
    // 失敗した理由が UI まで残る (これが読めないと「なぜ止まったか」が画面から消える)
    expect(state.error).toBe(sample.error);
    expect(state.error?.length).toBeGreaterThan(0);
  },
};

function renderConnected() {
  const view = renderHook(() => useRobotSocket(URL));
  act(() => latestSocket().open());
  return view;
}

beforeEach(() => {
  installMockWebSocket();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("WS 契約 (ws-contract.json)", () => {
  it("契約の全サンプルに TS 側の検証がある", () => {
    expect(Object.keys(SAMPLES).toSorted()).toEqual(Object.keys(EXPECTATIONS).toSorted());
  });

  it("state サンプルに UI が読むフィールドが揃っている", () => {
    for (const field of STATE_FIELDS_UI_READS) {
      expect(SAMPLES.state).toHaveProperty(field);
    }
  });

  describe.each(Object.keys(SAMPLES))("%s", (name) => {
    it("受信経路を通って状態へ反映される", () => {
      const expectation = EXPECTATIONS[name];
      if (!expectation) throw new Error(`契約サンプル ${name} に対応する検証がありません`);

      const { result } = renderConnected();
      act(() => latestSocket().receive(SAMPLES[name]));

      expectation(result.current, SAMPLES[name]);
    });
  });
});

type FieldUse = "ui" | "parser" | { unused: string };

type FieldSpec = Record<string, FieldUse>;

function fieldsOf<T>(spec: Record<keyof T & string, FieldUse>): FieldSpec {
  return spec;
}

function nest(prefix: string, spec: FieldSpec): FieldSpec {
  return Object.fromEntries(Object.entries(spec).map(([key, use]) => [`${prefix}.${key}`, use]));
}

type Wire<T> = T & { type: string };
type WireOf<K extends ServerMessage["type"]> = Extract<ServerMessage, { type: K }>;

const MOTOR_STATE = fieldsOf<MotorState>({
  pos: "ui",
  vel: "ui",
  torque: "ui",
  temp: "ui",
  command: "ui",
  command_mode: "ui",
});

const BUS_HEALTH = fieldsOf<BusHealth>({
  name: "ui",
  channel: "ui",
  state: "ui",
  tx_error_count: "ui",
  rx_error_count: "ui",
  bus_off: "ui",
  rx_down: "ui",
  rx_down_episodes: "ui",
  may_affect_workpiece: "ui",
  last_tx_at: { unused: "鮮度の判定はサーバーが済ませて state に畳んである" },
  last_rx_at: { unused: "同上。UI が閾値を持つと判定が 2 箇所になる" },
});

const MOTOR_HEALTH = fieldsOf<MotorHealth>({
  name: "ui",
  state: "ui",
  feedback_age_ms: "ui",
  bus: { unused: "モータ行はテレメトリ側と名前で突き合わせる" },
  last_feedback_at: { unused: "経過時間 (feedback_age_ms) の方を出す" },
  temperature: { unused: "温度はテレメトリの motors[].temp を唯一の表示元にする" },
  detail: "ui",
});

const HEALTH = fieldsOf<HealthSnapshot>({
  overall: "ui",
  buses: "ui",
  motors: "ui",
  detail: "ui",
  timestamp: { unused: "配信時刻。モータの鮮度は feedback_age_ms が持つ" },
});

const POSITION_LOOP = fieldsOf<PositionLoopState>({
  bus: "ui",
  running: "ui",
  paused: { unused: "動作確認中の意図的な停止なので異常に数えない" },
  sync_violations: { unused: "ラッチ軸は safety.sync_violations に集約されている" },
});

const SYNC_MONITOR = fieldsOf<SyncMonitorState>({
  axes: "ui",
  running: "ui",
  violated: { unused: "同上。ラッチ軸は safety.sync_violations を唯一の表示元にする" },
});

const LIMIT_MONITOR = fieldsOf<LimitMonitorState>({
  axes: "ui",
  running: "ui",
  stopped: {
    unused: "端で止めた軸。保護が効いた証であって異常ではないので安全機構の欄には出さない",
  },
});

const TARGET_REFRESHER = fieldsOf<TargetRefresherState>({
  motors: "ui",
  running: "ui",
  paused: { unused: "動作確認中の意図的な停止なので異常に数えない" },
});

const SENSOR_STATE = fieldsOf<SensorState>({
  active: "ui",
  stale: "ui",
});

const SAFETY = fieldsOf<SafetyState>({
  sync_violations: "ui",
  unenergized_motors: "ui",
  firmware_unconfirmed_motors: "ui",
  failed_tasks: "ui",
  reenergizing: "ui",
  loops_running: "ui",
  monitors_running: "ui",
  limit_monitors_running: "ui",
  refreshers_running: "ui",
  position_loops: "ui",
  sync_monitors: "ui",
  limit_monitors: "ui",
  target_refreshers: "ui",
});

const MANUAL_RANGE = fieldsOf<ManualRange>({
  min: "ui",
  max: "ui",
  steps: "ui",
});

const MANUAL_POSITION = fieldsOf<ManualPosition>({
  name: "ui",
  value: "ui",
});

const MANUAL_AXIS = fieldsOf<ManualAxis>({
  name: "ui",
  unit: "ui",
  value: "ui",
  target: "ui",
  manual: "ui",
  manual_always: "ui",
  deviation: "ui",
  sync_tolerance: "ui",
  positions: "ui",
  command_mode: "ui",
  motors: {
    unused: "軸行はモータ単位では操作させない (左右直結ペアが別々に動くと機構がねじれる)",
  },
});

const MANUAL = fieldsOf<ManualState>({
  mode: "ui",
  axes: "ui",
});

const EXCLUDED_STEP = fieldsOf<ExcludedStep>({
  step: "ui",
  missing_axes: "ui",
});

const SEQUENCE_FAILURE = fieldsOf<SequenceFailure>({
  step_index: "ui",
  step: "ui",
  message: "ui",
});

const STEP = fieldsOf<SequenceStepInfo>({
  index: "ui",
  label: "ui",
  require_trigger: "ui",
});

const MATCH_TIMER = fieldsOf<MatchTimer>({
  running: "ui",
  elapsed_ms: "ui",
  duration_ms: "ui",
});

const CHECKLIST_ITEM = fieldsOf<ChecklistItem>({
  id: "ui",
  label: "ui",
  checked: "ui",
  group: "ui",
});
const CHECKLIST_STATE = fieldsOf<ChecklistState>({ items: "ui", completed: "ui" });

const MOTOR_CHECK_FIELDS: FieldSpec = {
  ...fieldsOf<Wire<MotorCheckSnapshot>>({
    type: "parser",
    available: "ui",
    blocked_reason: "ui",
    running: "ui",
    current_step: "ui",
    step_index: "ui",
    total_steps: "ui",
    steps: "ui",
    error: "ui",
    last_error: "ui",
    excluded_steps: "ui",
  }),
  ...nest("steps[]", STEP),
  ...nest("last_error", SEQUENCE_FAILURE),
  ...nest("excluded_steps[]", EXCLUDED_STEP),
};

const HOMING_RESULT = fieldsOf<HomingAxisResult>({
  axis: "ui",
  error: "ui",
});

const HOMING_FIELDS: FieldSpec = {
  ...fieldsOf<Wire<HomingSnapshot>>({
    type: "parser",
    available: "ui",
    blocked_reason: "ui",
    running: "ui",
    robot: "ui",
    axes: "ui",
    current_axis: "ui",
    results: "ui",
    error: "ui",
    targets: "ui",
  }),
  ...nest("results[]", HOMING_RESULT),
  "targets.*": "ui",
};

const SWITCH_MEASUREMENT = fieldsOf<SwitchMeasurement>({
  axis: "ui",
  unit: "ui",
  direction: "ui",
  engage: "ui",
  release: "ui",
  width: "ui",
  step: "ui",
  coarse_step: "ui",
});

const SWITCH_MEASURE_FIELDS: FieldSpec = {
  ...fieldsOf<Wire<SwitchMeasureSnapshot>>({
    type: "parser",
    available: "ui",
    blocked_reason: "ui",
    running: "ui",
    robot: "ui",
    axis: "ui",
    direction: "ui",
    result: "ui",
    error: "ui",
    targets: "ui",
  }),
  ...nest("result", SWITCH_MEASUREMENT),
  "targets.*": "ui",
};

const SUCTION = fieldsOf<SuctionState>({
  pads: "ui",
});

const SUCTION_PAD = fieldsOf<SuctionPad>({
  axis: "ui",
  label: "ui",
  enabled: "ui",
});

const STATE_FIELDS: FieldSpec = {
  ...fieldsOf<RobotState>({
    type: "parser",
    robot: "ui",
    sequence: "ui",
    step_index: "ui",
    total_steps: "ui",
    waiting_trigger: "ui",
    running: "ui",
    steps: "ui",
    motors: "ui",
    sensors: "ui",
    e_stop_active: "ui",
    health: "ui",
    safety: "ui",
    manual: "ui",
    suction: "ui",
    last_error: "ui",
    current_step: { unused: "現在ステップ名は steps[step_index].label を唯一の表示元にする" },
  }),
  ...nest("suction", SUCTION),
  ...nest("suction.pads[]", SUCTION_PAD),
  ...nest("motors.*", MOTOR_STATE),
  ...nest("sensors.*", SENSOR_STATE),
  ...nest("health", HEALTH),
  ...nest("health.buses[]", BUS_HEALTH),
  ...nest("health.motors[]", MOTOR_HEALTH),
  ...nest("safety", SAFETY),
  ...nest("safety.position_loops[]", POSITION_LOOP),
  ...nest("safety.sync_monitors[]", SYNC_MONITOR),
  ...nest("safety.limit_monitors[]", LIMIT_MONITOR),
  ...nest("safety.target_refreshers[]", TARGET_REFRESHER),
  ...nest("steps[]", STEP),
  ...nest("manual", MANUAL),
  ...nest("manual.axes[]", MANUAL_AXIS),
  ...nest("manual.axes[].manual", MANUAL_RANGE),
  ...nest("manual.axes[].positions[]", MANUAL_POSITION),
  ...nest("last_error", SEQUENCE_FAILURE),
};

const E_STOP_FIELDS = fieldsOf<WireOf<"e_stop_state">>({
  type: "parser",
  active: "ui",
  reason: "ui",
});

const HEALTH_CHANGE_FIELDS = fieldsOf<Wire<HealthChange>>({
  type: "parser",
  robot: "ui",
  level: "ui",
  target: "ui",
  from: "ui",
  to: "ui",
  message: "ui",
});

const DECLARED: Record<string, FieldSpec> = {
  state: STATE_FIELDS,
  state_with_last_error: STATE_FIELDS,

  server_info: fieldsOf<Wire<ServerInfo>>({
    type: "parser",
    dev_tools: "ui",
    dry_run: { unused: "現状 UI では表示しない (health の STALE で分かる)" },
    temp_warning_c: "ui",
    temp_critical_c: "ui",
  }),

  match_state: {
    ...fieldsOf<Wire<MatchState>>({
      type: "parser",
      court: "ui",
      phase: "ui",
      can_start_match: "ui",
      checklists: "ui",
      timer: "ui",
    }),
    ...nest("timer", MATCH_TIMER),
    ...nest("checklists.*", CHECKLIST_STATE),
    ...nest("checklists.*.items[]", CHECKLIST_ITEM),
  },

  e_stop_state: E_STOP_FIELDS,
  e_stop_state_with_reason: E_STOP_FIELDS,
  health_change: HEALTH_CHANGE_FIELDS,
  health_change_bus: HEALTH_CHANGE_FIELDS,

  command_rejected: fieldsOf<WireOf<"command_rejected">>({
    type: "parser",
    command: "ui",
    reason: "ui",
  }),

  motor_check_state: MOTOR_CHECK_FIELDS,
  motor_check_state_with_exclusions: MOTOR_CHECK_FIELDS,
  homing_state: HOMING_FIELDS,
  switch_measure_state: SWITCH_MEASURE_FIELDS,
  switch_measure_state_with_result: SWITCH_MEASURE_FIELDS,
  switch_measure_state_with_error: SWITCH_MEASURE_FIELDS,
};

const DYNAMIC_MAPS = new Set(["motors", "sensors", "checklists", "targets"]);

function flattenPaths(value: unknown, prefix = ""): string[] {
  if (Array.isArray(value)) return value.flatMap((item) => flattenPaths(item, `${prefix}[]`));
  if (typeof value !== "object" || value === null) return [];

  const dynamic = DYNAMIC_MAPS.has(prefix.split(".").pop() ?? "");
  return Object.entries(value).flatMap(([key, child]) => {
    const path = dynamic ? `${prefix}.*` : prefix === "" ? key : `${prefix}.${key}`;
    return dynamic ? flattenPaths(child, path) : [path, ...flattenPaths(child, path)];
  });
}

const SAMPLE_OMITS: Record<string, Record<string, string>> = {
  e_stop_state: {
    reason: "理由なしで停止した形。理由付きの配信は e_stop_state_with_reason が受け持つ",
  },
};

function parentOf(path: string): string {
  const cut = path.lastIndexOf(".");
  return cut < 0 ? "" : path.slice(0, cut);
}

function missingDeclaredPaths(name: string, declared: FieldSpec, sample: Sample): string[] {
  const present = new Set(flattenPaths(sample));
  const expanded = new Set(["", ...[...present].map(parentOf)]);
  const omitted = SAMPLE_OMITS[name] ?? {};
  return Object.entries(declared)
    .filter(([path, use]) => use === "ui" && expanded.has(parentOf(path)) && !present.has(path))
    .map(([path]) => path)
    .filter((path) => !(path in omitted))
    .toSorted();
}

describe("WS 契約 (逆方向 — サーバーが送るものを TS が知っているか)", () => {
  it("全サンプルに宣言がある", () => {
    expect(Object.keys(SAMPLES).toSorted()).toEqual(Object.keys(DECLARED).toSorted());
  });

  describe.each(Object.keys(SAMPLES))("%s", (name) => {
    it("実配信の全フィールドが TS 側の型と用途を持つ", () => {
      const declared = DECLARED[name];
      const undeclared = [...new Set(flattenPaths(SAMPLES[name]))]
        .filter((path) => !(path in declared))
        .toSorted();

      expect(undeclared).toEqual([]);
    });

    it("UI が読むと宣言した欄が実配信から消えていない", () => {
      expect(missingDeclaredPaths(name, DECLARED[name], SAMPLES[name])).toEqual([]);
    });
  });

  it("サンプルに載らないと決めた欄には理由が書いてあり、実際に載っていない", () => {
    for (const [name, omits] of Object.entries(SAMPLE_OMITS)) {
      const present = new Set(flattenPaths(SAMPLES[name]));
      for (const [path, reason] of Object.entries(omits)) {
        expect(reason.length, `${name}.${path}`).toBeGreaterThan(0);
        expect(present.has(path), `${name}.${path} は実配信に載っている`).toBe(false);
      }
    }
  });

  it("使わないと決めたフィールドには理由が書いてある", () => {
    for (const [name, spec] of Object.entries(DECLARED)) {
      for (const [field, use] of Object.entries(spec)) {
        if (typeof use === "object") {
          expect(use.unused.length, `${name}.${field}`).toBeGreaterThan(0);
        }
      }
    }
  });
});
