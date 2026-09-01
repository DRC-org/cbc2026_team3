import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useRobotSocket } from "@/hooks/useRobotSocket";
import type {
  BusHealth,
  ChecklistItem,
  ChecklistState,
  HealthChange,
  HealthSnapshot,
  ManualAxis,
  ManualRange,
  ManualState,
  MatchState,
  MatchTimer,
  MotorCheckSnapshot,
  MotorHealth,
  MotorPid,
  MotorState,
  PositionLoopState,
  RobotState,
  SafetyState,
  SequenceFailure,
  SequenceStepInfo,
  ServerInfo,
  ServerMessage,
  SyncMonitorState,
  TargetRefresherState,
  TuningAdvice,
  TuningCapture,
  TuningMetrics,
  TuningSamples,
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
  // 安全機構は 8 欄すべてを読む。1 欄でも落ちれば `describeSafetyIssues` が
  // 「安全機構 判定不能」へ倒れ、ラッチ軸も保護ループの生死も画面から消える
  "safety.sync_violations",
  "safety.unenergized_motors",
  "safety.loops_running",
  "safety.monitors_running",
  "safety.position_loops",
  "safety.sync_monitors",
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

  /**
   * 失敗して止まった直後の配信。**この形が受信経路を通ることが本題** ——
   * 通らないと、左右ずれで止まっても画面は「待機中」へ戻るだけになり、
   * 3 層保護の第 1 層が操縦者から無音になる。
   */
  state_with_last_error: (result, sample) => {
    const robot = sample.robot as string;
    expect(result.states[robot].last_error).toEqual(sample.last_error);
    // 理由だけでなく「どこで止まったか」まで残す (今の機体の姿勢に直結する)
    expect(result.states[robot].last_error?.step.length).toBeGreaterThan(0);
    expect(result.states[robot].last_error?.message.length).toBeGreaterThan(0);
  },

  server_info: (result, sample) => {
    // 開発用ボタンの表示可否はこの 1 通だけが決める。受信条件が弾くと
    // 「--dev-tools で起動したのにボタンが出ない」が型検査を通ったまま成立する
    // 温度しきい値も同じ 1 通で届く。受信条件が弾くと UI 側は「未取得」に倒れ、
    // 温度の色分けが config を変えても一切出ないまま型検査だけ通る
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
    // 「誰かが押したのか、機体が壊れたのか」を操縦者が区別できないと復旧手順を選べない
    expect(result.eStopReason).toBe(sample.reason);
  },

  command_rejected: (result, sample) => {
    expect(result.rejection).toMatchObject({
      command: sample.command,
      reason: sample.reason,
    });
  },

  motor_check_state: (result, sample) => {
    // **robot を持たない 1 通**。受信条件が robot を要求していると 100% 捨てられる
    expect(sample.robot).toBeUndefined();

    const state = result.motorCheck;
    expect(state.available).toBe(sample.available);
    expect(state.running).toBe(sample.running);
    expect(state.error).toBe(sample.error);
    expect(state.total_steps).toBe(sample.total_steps);
    expect(state.steps).toEqual(sample.steps);
  },

  tuning_capture: (result, sample) => {
    const captures = result.tuningCaptures[`${sample.robot}/${sample.motor}`];
    expect(captures).toHaveLength(1);

    // 波形・指標・助言は 1 通で運ぶ。受信条件がどれか 1 つを弾くと、
    // 残り 2 つだけが画面に出て食い違ったまま固まる
    expect(captures[0].samples).toEqual(sample.samples);
    expect(captures[0].metrics).toEqual(sample.metrics);
    expect(captures[0].advice).toEqual(sample.advice);
    expect(captures[0].gains).toEqual(sample.gains);
    // 実配信には指標も助言も載っている。空で通ると「受信できている」の検証にならない
    expect(captures[0].metrics).not.toBeNull();
    expect(captures[0].advice.length).toBeGreaterThan(0);
  },

  tuning_capture_not_a_step: (result, sample) => {
    const captures = result.tuningCaptures[`${sample.robot}/${sample.motor}`];
    expect(captures).toHaveLength(1);
    // **metrics が null の形も受信経路を通ること。** 指標を出せなかった記録を
    // 弾いてしまうと、波形だけは見たい場面 (何が起きたか確かめたい) で何も出ない
    expect(captures[0].metrics).toBeNull();
    expect(captures[0].advice).toEqual([]);
    expect(captures[0].samples).toEqual(sample.samples);
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

const MOTOR_PID = fieldsOf<MotorPid>({
  kp: "ui",
  ki: "ui",
  kd: "ui",
  applies_to: "ui",
});

const MOTOR_STATE = fieldsOf<MotorState>({
  pos: "ui",
  vel: "ui",
  torque: "ui",
  temp: "ui",
  // 落ちれば /pid-tuning は現在値を知る手段を失い、初期値 0 のまま送って
  // 全ゲインを 0 で潰す経路が戻る
  pid: "ui",
  // 落ちれば画面から偏差そのものが消える。調整で最も見たい量が
  // 操縦者の頭の中の引き算にしか存在しない状態へ戻る
  target: "ui",
  // 落ちれば「ゲインを変えても応答が変わらない」理由が画面から読めなくなる
  saturated: "ui",
});

const BUS_HEALTH = fieldsOf<BusHealth>({
  name: "ui",
  channel: "ui",
  state: "ui",
  tx_error_count: "ui",
  rx_error_count: "ui",
  bus_off: "ui",
  rx_down: "ui",
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
  // 累積角の再アンカーなど、状態 (OK/STALE) では表せない事情の唯一の出口。
  // 落とすと「原点がずれた」ことが操縦者にどこからも見えなくなる
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

/** 目標値再送タスク 1 本 (= 自作モタドラ向け 20Hz の再送) の状態 */
const TARGET_REFRESHER = fieldsOf<TargetRefresherState>({
  motors: "ui",
  running: "ui",
  paused: { unused: "動作確認中の意図的な停止なので異常に数えない" },
});

const SAFETY = fieldsOf<SafetyState>({
  sync_violations: "ui",
  unenergized_motors: "ui",
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

/** 失敗したステップと理由。`state` と `motor_check_state` の双方に載る */
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
  // 項目をどのコントロールの隣に置くか (lib/checklistGroups.ts が対応表を持つ)
  group: "ui",
});
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
    // シーケンスが落ちた理由 (左右ずれ・到達しない等)。これが無い間、保護が
    // 効いて止まっても画面は「待機中」へ戻るだけで、操縦者から無音だった
    last_error: "ui",
    current_step: { unused: "現在ステップ名は steps[step_index].label を唯一の表示元にする" },
  }),
  ...nest("motors.*", MOTOR_STATE),
  ...nest("motors.*.pid", MOTOR_PID),
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

const TUNING_METRICS = fieldsOf<TuningMetrics>({
  step_from: "ui",
  step_to: "ui",
  step_size: "ui",
  rise_time_s: "ui",
  overshoot_pct: "ui",
  peak_time_s: "ui",
  settling_time_s: "ui",
  steady_state_error: "ui",
  oscillation_hz: "ui",
  damping_ratio: "ui",
  saturation_ratio: "ui",
  peak_output: "ui",
  settle_band: "ui",
  sample_count: { unused: "解析に使った点数。波形の見た目からは読めないが判断は変えない" },
  duration_s: "ui",
});

const TUNING_ADVICE = fieldsOf<TuningAdvice>({
  code: { unused: "UI は message をそのまま出す。code はテストが名前で参照するための識別子" },
  severity: "ui",
  message: "ui",
});

const TUNING_SAMPLES = fieldsOf<TuningSamples>({
  t: "ui",
  target: "ui",
  pos: "ui",
  output: "ui",
  sat: "ui",
});

const TUNING_CAPTURE_FIELDS: FieldSpec = {
  ...fieldsOf<Wire<TuningCapture>>({
    type: "parser",
    robot: "ui",
    motor: "ui",
    captured_at: "ui",
    gains: "ui",
    metrics: "ui",
    advice: "ui",
    samples: "ui",
  }),
  ...nest("gains", fieldsOf<TuningCapture["gains"]>({ kp: "ui", ki: "ui", kd: "ui" })),
  ...nest("metrics", TUNING_METRICS),
  ...nest("advice[]", TUNING_ADVICE),
  ...nest("samples", TUNING_SAMPLES),
};

/** サンプル名 → そのメッセージが持ちうる全フィールド (ドット区切り、配列要素は `[]`) */
const DECLARED: Record<string, FieldSpec> = {
  state: STATE_FIELDS,
  // 失敗して止まった形。**null 側だけを契約に載せると、`last_error` の中身を
  // UI が読めているかを誰も確かめない** (metrics が null の記録を別サンプルで
  // 持っているのと同じ理由)
  state_with_last_error: STATE_FIELDS,

  server_info: fieldsOf<Wire<ServerInfo>>({
    type: "parser",
    dev_tools: "ui",
    // 機体が繋がっていないことは health 側 (モータの STALE) に出るため、
    // ここでは表示に使わない。将来 UI に「dry-run 中」を出すならここを "ui" にする
    dry_run: { unused: "現状 UI では表示しない (health の STALE で分かる)" },
    // モータ温度の色分けの境界。UI 側に定数を持つと config を変えても画面だけが
    // 古い境界で判定するため、サーバーの config を唯一の出どころにしてある
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

  // ワイヤ形式と正規化後の形が違う唯一のメッセージ。受信時に `motorCheck` で
  // 包み直しているので、`WireOf` ではなく素のペイロード型で宣言する
  motor_check_state: {
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
      // 失敗理由のもう 1 つの置き場所。`error` と合わせて 1 つへ畳んで出す
      // (`lib/motorCheckStatus.ts`)。片方だけを読むと、サーバーが置き場所を
      // 変えた瞬間に失敗が「未実行」と同じ表示へ落ちる
      last_error: "ui",
    }),
    ...nest("steps[]", STEP),
    ...nest("last_error", SEQUENCE_FAILURE),
  },

  // 波形・指標・助言を 1 通で運ぶ。motor_check_state と同じく受信時に `capture` で
  // 包み直しているので、素のペイロード型で宣言する
  tuning_capture: TUNING_CAPTURE_FIELDS,
  // 指標を出せなかった記録。**この形も宣言に含める** — metrics が null の側だけ
  // 契約から漏れると、UI が null を弾く条件を書いても誰も気付けない
  tuning_capture_not_a_step: TUNING_CAPTURE_FIELDS,
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

/**
 * そのサンプルには正当に載らない欄。**理由を必須にする。**
 *
 * 同じメッセージ型が 2 つの形で配信されることがある (理由付き / 理由なしの
 * 緊急停止)。例外を無条件に許すと「サーバーが落とした欄」と「元からこの形には
 * 無い欄」の区別が付かなくなるので、`unused` と同じく理由を書かせる。
 */
const SAMPLE_OMITS: Record<string, Record<string, string>> = {
  e_stop_state: {
    reason: "理由なしで停止した形。理由付きの配信は e_stop_state_with_reason が受け持つ",
  },
};

/** ドット区切りパスの親。トップレベルの親は根 (`""`) */
function parentOf(path: string): string {
  const cut = path.lastIndexOf(".");
  return cut < 0 ? "" : path.slice(0, cut);
}

/**
 * 宣言に `"ui"` と書いたのにサンプルから消えている欄を挙げる。
 *
 * **逆方向の突き合わせだけでは足りない。** あちらは「宣言に無いパスがサンプルに在る」
 * ことしか見ないので、**宣言にあるのにサンプルから消えた欄**は素通りする。実際に
 * `safety` の 6 欄はどの存在検査にも載っておらず、サーバーが 1 欄落として契約を
 * 焼き直しても Python も TS も全テスト緑のまま、UI は起動直後に白画面になった。
 *
 * 親が null / 空配列のサンプルでは子が無いのが正しい (`metrics` が null の記録、
 * `manual` を持たない軸)。親が 1 度でも展開されている場合だけ子を要求する。
 */
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

      // 落ちたら、サーバーが送り始めた欄を UI が知らないということ。
      // 型へ足して読むか、読まないなら unused に理由を書いて明示する
      expect(undeclared).toEqual([]);
    });

    it("UI が読むと宣言した欄が実配信から消えていない", () => {
      // 落ちたら、UI が読んでいる欄をサーバーが送らなくなったということ。
      // 表示が黙って空になるだけでは済まず、`.length` / `.filter` を呼ぶ側は
      // レンダー本体で投げて React ツリーごと落ちる (ヘッダーの緊急停止ボタンごと)
      expect(missingDeclaredPaths(name, DECLARED[name], SAMPLES[name])).toEqual([]);
    });
  });

  it("サンプルに載らないと決めた欄には理由が書いてあり、実際に載っていない", () => {
    for (const [name, omits] of Object.entries(SAMPLE_OMITS)) {
      const present = new Set(flattenPaths(SAMPLES[name]));
      for (const [path, reason] of Object.entries(omits)) {
        expect(reason.length, `${name}.${path}`).toBeGreaterThan(0);
        // 例外が古くなって残っていると、その欄だけ存在検査が永久に効かなくなる
        expect(present.has(path), `${name}.${path} は実配信に載っている`).toBe(false);
      }
    }
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
