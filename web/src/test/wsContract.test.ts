import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useRobotSocket } from "@/hooks/useRobotSocket";
import type {
  BusHealth,
  CheckRunSnapshot,
  ChecklistItem,
  ChecklistState,
  HealthChange,
  HealthSnapshot,
  ManualAxis,
  ManualRange,
  ManualState,
  MatchState,
  MatchTimer,
  MotorCheckRecord,
  MotorHealth,
  MotorState,
  PositionLoopState,
  RobotState,
  SafetyState,
  SequenceStepInfo,
  ServerMessage,
  SyncMonitorState,
  TargetRefresherState,
} from "@/lib/protocol";
import { installMockWebSocket, latestSocket } from "@/test/mockWebSocket";
import contract from "@/test/ws-contract.json";

/**
 * サーバーの実配信 (`ws-contract.json`) が UI の受信経路を通ることを固定する。
 *
 * 型アサーションだけでは足りない。型が合っていても `useRobotSocket` の受信条件が
 * 弾けばメッセージは捨てられ、画面には何も出ない。実際に `health_change` は
 * `robot` を含まないまま配信されており、UI は `typeof msg.robot === "string"` を
 * 受信条件にしていたので実機で 100% 捨てられていた。両側のテストが揃って
 * 見逃したのは、TS 側がサンプルを自分で捏造していたからである。
 *
 * したがってここでは **契約ファイルの実サンプルを reducer に流し込み**、
 * 状態が期待どおり更新されることだけを検証する。サンプルを手で書き写してはならない
 * (写した瞬間に「想像した契約」へ逆戻りする)。
 */

const URL = "ws://contract/ws";

type Sample = Record<string, unknown>;

const SAMPLES = contract.samples as unknown as Record<string, Sample>;
const EPOCH_SECONDS: number = contract.$placeholders.epoch_seconds;

type SocketResult = ReturnType<typeof useRobotSocket>;
type Expectation = (result: SocketResult, sample: Sample) => void;

/**
 * state サンプルから UI が実際に読むフィールド。欠けたら画面のどこかが黙って壊れる。
 * 入れ子はドット区切り (`toHaveProperty` のパス指定) で書く。
 */
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
  "e_stop_active",
  "health",
  "safety",
  // 20Hz の目標値再送が止まると 500ms 後に generic アクチュエータが全停止する。
  // 配信から落ちれば画面はグリッパ・コンベアの無反応を説明できなくなる
  "safety.refreshers_running",
  "safety.target_refreshers",
  // 手動操縦。落ちれば操作モードの表示も軸一覧も出せなくなる
  "manual",
  "manual.mode",
  "manual.axes",
] as const;

/**
 * サンプル名 → 受信後の期待。
 *
 * 契約ファイルにメッセージ型が増えると「未対応のサンプルがある」テストが落ちる。
 * サーバーが送り始めたものを UI が黙って捨て続ける状態を、ここで検出する。
 */
const EXPECTATIONS: Record<string, Expectation> = {
  state: (result, sample) => {
    const robot = sample.robot as string;
    // 受信した state はそのまま保持される (モータ名等をハードコードしないため)
    expect(result.states[robot]).toEqual(sample);
    expect(result.eStopActive).toBe(sample.e_stop_active);
    // 実行状態は推測せずサーバーの running をそのまま持つ
    expect(result.states[robot].running).toBe(sample.running);
    // 安全機構 (ラッチ中の軸・保護ループの生死) も配信そのまま
    expect(result.states[robot].safety).toEqual(sample.safety);
    // 操作モードと軸一覧。**軸名を UI 側へ書かないため配信をそのまま持つ**
    expect(result.states[robot].manual).toEqual(sample.manual);
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
    // 「誰かが押したのか、機体が壊れたのか」を操縦者が区別できないと復旧手順を選べない
    expect(result.eStopReason).toBe(sample.reason);
  },

  command_rejected: (result, sample) => {
    expect(result.rejection).toMatchObject({
      command: sample.command,
      reason: sample.reason,
    });
  },

  motor_check_progress: (result, sample) => {
    const state = result.motorChecks[sample.robot as string];
    expect(state.status).toBe("running");
    expect(state.current).toBe(sample.current);
    expect(state.progress).toEqual({ index: sample.index, total: sample.total });
  },

  motor_check_record: (result, sample) => {
    const state = result.motorChecks[sample.robot as string];
    expect(state.records).toEqual([sample.record]);
  },

  motor_check_done: (result, sample) => {
    const snapshot = sample.snapshot as Record<string, unknown>;
    const state = result.motorChecks[sample.robot as string];
    expect(state.status).toBe("completed");
    expect(state.snapshot).toEqual(snapshot);
    expect(state.records).toEqual(snapshot.records);
    // サーバーはエポック秒、Date はミリ秒。ここが取り違えられると実施時刻が 1970 年になる
    expect(state.finishedAtMs).toBe(EPOCH_SECONDS * 1000);
    expect(state.startedAtMs).toBe(EPOCH_SECONDS * 1000);
  },

  motor_check_error: (result, sample) => {
    const state = result.motorChecks[sample.robot as string];
    expect(state.status).toBe("error");
    expect(state.error).toBe(sample.message);
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
    // サーバーが新しいメッセージ型を送り始めたのに UI が対応していない状態を、
    // 契約ファイルの再生成 (UPDATE_WS_CONTRACT=1) 時点で落とす
    expect(Object.keys(SAMPLES).toSorted()).toEqual(Object.keys(EXPECTATIONS).toSorted());
  });

  it("state サンプルに UI が読むフィールドが揃っている", () => {
    // 型は実行時に消えるので、フィールドの存在はここでしか守れない。
    // 例えば running が配信から落ちれば、UI は再び step_index からの推測へ逆戻りする
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

/**
 * --- 逆方向の突き合わせ -------------------------------------------------
 *
 * 上のサンプル別検証は「UI が読む値が実配信に在るか」しか見ない。逆に
 * **サーバーが送っているのに TS 側が知らないフィールド**は素通りする。
 * 実際に `health.detail` (サーバーが「健全性を判定できなかった理由」を載せる欄) は
 * 型にすら無く、UI は overall=down の判定不能を「異常なし」と表示していた。
 *
 * ここでは実配信サンプルのキーを再帰的に列挙し、下の宣言と突き合わせる。
 *
 * **「サーバーの全フィールドを UI が消費する」ことまでは要求しない。** 表示に
 * 使い道の無い値 (配信時刻、名前で突き合わせ済みのバス名) は実際に存在し、
 * 無理に画面へ出すと「平常時に静かで、異常時に主張する」原則の方が壊れる。
 * 要求するのは **知らないフィールドが存在しないこと** — 使わないなら
 * `unused` に理由を書いて明示する。理由の書かれていない欄が増えたらここが落ちる。
 */
type FieldUse = "ui" | "parser" | { unused: string };

type FieldSpec = Record<string, FieldUse>;

/**
 * TS の型 1 つぶんの宣言。`Record<keyof T, ...>` なので、型にあるキーを
 * 書き忘れても、型に無いキーを書いても tsc が落ちる。
 * 「TS 側が型として持っているか」の確認を型検査へ肩代わりさせる。
 */
function fieldsOf<T>(spec: Record<keyof T & string, FieldUse>): FieldSpec {
  return spec;
}

/** 入れ子のオブジェクト 1 つぶんを接頭辞付きで畳み込む */
function nest(prefix: string, spec: FieldSpec): FieldSpec {
  return Object.fromEntries(Object.entries(spec).map(([key, use]) => [`${prefix}.${key}`, use]));
}

/** ワイヤ形式 = ペイロードの型 + エンベロープの `type` 欄 */
type Wire<T> = T & { type: string };
/** 正規化後の型がそのままワイヤ形式と 1:1 のメッセージ */
type WireOf<K extends ServerMessage["type"]> = Extract<ServerMessage, { type: K }>;

const MOTOR_STATE = fieldsOf<MotorState>({
  pos: "ui",
  vel: "ui",
  torque: "ui",
  temp: "ui",
});

const BUS_HEALTH = fieldsOf<BusHealth>({
  name: "ui",
  channel: "ui",
  state: "ui",
  tx_error_count: "ui",
  rx_error_count: "ui",
  bus_off: "ui",
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
  detail: { unused: "モータ個別の補足はどの画面も出していない" },
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

/** 目標値再送タスク 1 本 (= 自作モタドラ向け 20Hz の再送) の状態 */
const TARGET_REFRESHER = fieldsOf<TargetRefresherState>({
  motors: "ui",
  running: "ui",
  paused: { unused: "動作確認中の意図的な停止なので異常に数えない" },
});

const SAFETY = fieldsOf<SafetyState>({
  sync_violations: "ui",
  loops_running: "ui",
  monitors_running: "ui",
  refreshers_running: "ui",
  position_loops: "ui",
  sync_monitors: "ui",
  target_refreshers: "ui",
});

const MANUAL_RANGE = fieldsOf<ManualRange>({
  min: "ui",
  max: "ui",
  steps: "ui",
});

const MANUAL_AXIS = fieldsOf<ManualAxis>({
  name: "ui",
  unit: "ui",
  value: "ui",
  target: "ui",
  manual: "ui",
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

const STEP = fieldsOf<SequenceStepInfo>({
  index: "ui",
  label: "ui",
  require_trigger: "ui",
});

const CHECK_RECORD = fieldsOf<MotorCheckRecord>({
  motor: "ui",
  bus: "ui",
  result: "ui",
  expected: "ui",
  observed: "ui",
  detail: "ui",
  started_at: { unused: "項目ごとの所要時間は出していない。実施時刻は run 単位で足りる" },
  finished_at: { unused: "同上" },
});

const CHECK_SNAPSHOT = fieldsOf<CheckRunSnapshot>({
  overall: "ui",
  records: "ui",
  started_at: "ui",
  finished_at: "ui",
  robot: { unused: "宛先はエンベロープの robot で決まる (同じ値を二重に持っている)" },
});

const MATCH_TIMER = fieldsOf<MatchTimer>({
  running: "ui",
  elapsed_ms: "ui",
  duration_ms: "ui",
});

const CHECKLIST_ITEM = fieldsOf<ChecklistItem>({ id: "ui", label: "ui", checked: "ui" });
const CHECKLIST_STATE = fieldsOf<ChecklistState>({ items: "ui", completed: "ui" });

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
    e_stop_active: "ui",
    health: "ui",
    safety: "ui",
    manual: "ui",
    current_step: { unused: "現在ステップ名は steps[step_index].label を唯一の表示元にする" },
  }),
  ...nest("motors.*", MOTOR_STATE),
  ...nest("health", HEALTH),
  ...nest("health.buses[]", BUS_HEALTH),
  ...nest("health.motors[]", MOTOR_HEALTH),
  ...nest("safety", SAFETY),
  ...nest("safety.position_loops[]", POSITION_LOOP),
  ...nest("safety.sync_monitors[]", SYNC_MONITOR),
  ...nest("safety.target_refreshers[]", TARGET_REFRESHER),
  ...nest("steps[]", STEP),
  ...nest("manual", MANUAL),
  ...nest("manual.axes[]", MANUAL_AXIS),
  ...nest("manual.axes[].manual", MANUAL_RANGE),
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

/** サンプル名 → そのメッセージが持ちうる全フィールド (ドット区切り、配列要素は `[]`) */
const DECLARED: Record<string, FieldSpec> = {
  state: STATE_FIELDS,

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

  motor_check_progress: fieldsOf<WireOf<"motor_check_progress">>({
    type: "parser",
    robot: "ui",
    current: "ui",
    index: "ui",
    total: "ui",
  }),

  motor_check_record: {
    ...fieldsOf<WireOf<"motor_check_record">>({ type: "parser", robot: "ui", record: "ui" }),
    ...nest("record", CHECK_RECORD),
  },

  motor_check_done: {
    ...fieldsOf<WireOf<"motor_check_done">>({ type: "parser", robot: "ui", snapshot: "ui" }),
    ...nest("snapshot", CHECK_SNAPSHOT),
    ...nest("snapshot.records[]", CHECK_RECORD),
  },

  motor_check_error: fieldsOf<WireOf<"motor_check_error">>({
    type: "parser",
    robot: "ui",
    message: "ui",
  }),
};

/**
 * キー名が動的なマップ。ここを普通の入れ子として辿ると `motors.gripper.pos` の形で
 * モータ名が契約へ焼き付き、「UI はモータ名をハードコードしない」設計と食い違う。
 */
const DYNAMIC_MAPS = new Set(["motors", "checklists"]);

/** サンプル 1 通のキーをドット区切りのパスへ平坦化する (配列要素はまとめて `[]`) */
function flattenPaths(value: unknown, prefix = ""): string[] {
  if (Array.isArray(value)) return value.flatMap((item) => flattenPaths(item, `${prefix}[]`));
  if (typeof value !== "object" || value === null) return [];

  const dynamic = DYNAMIC_MAPS.has(prefix.split(".").pop() ?? "");
  return Object.entries(value).flatMap(([key, child]) => {
    const path = dynamic ? `${prefix}.*` : prefix === "" ? key : `${prefix}.${key}`;
    // 動的マップの下はキー名ではなく形だけを契約にする
    return dynamic ? flattenPaths(child, path) : [path, ...flattenPaths(child, path)];
  });
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

      // 落ちたら、サーバーが送り始めた欄を UI が知らないということ。
      // 型へ足して読むか、読まないなら unused に理由を書いて明示する
      expect(undeclared).toEqual([]);
    });
  });

  it("使わないと決めたフィールドには理由が書いてある", () => {
    // 理由の無い unused は「消費し忘れ」と区別が付かない
    for (const [name, spec] of Object.entries(DECLARED)) {
      for (const [field, use] of Object.entries(spec)) {
        if (typeof use === "object") {
          expect(use.unused.length, `${name}.${field}`).toBeGreaterThan(0);
        }
      }
    }
  });
});
