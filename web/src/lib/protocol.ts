import type { EpochSeconds } from "@/lib/time";

/**
 * サーバー (`lib/server.py`) が WebSocket で配信するメッセージの型と受信条件。
 *
 * ここは最下層に置く (UI の hook を import しない)。以前は WS の型が
 * `hooks/useRobotSocket.ts` にあり、`lib/healthVerdict.ts` や `lib/phase.ts` が
 * hooks を import する依存の逆転が起きていた。
 *
 * 契約の正はサーバー側で、`test/ws-contract.json` (Python が生成) を
 * `test/wsContract.test.ts` がこの受信経路へ流し込んで突き合わせている。
 * 受信条件を厳しくするときは必ずそのテストで実配信を確認すること
 * (型が合っていても条件が弾けば画面には何も出ない)。
 */

/**
 * 受信条件を満たさなかったペイロードの印。
 *
 * **欠けた欄を既定値で埋めてはならない。** 空配列や 0 で埋めると「ラッチしているのに
 * 画面は平常」「測っていないのに測ったように見える値」へ化け、しかも埋めたことは
 * 画面のどこからも読めない。読めなかったことを 1 つの値で表し、表示側は
 * サーバーの `overall=down` を「健全性 判定不能」へ倒すのと同じ扱い (異常側) にする。
 *
 * **未配信 (undefined) とは区別する。** 未受信は異常にしない — 届いていないことと
 * 届いたものが読めないことは、操縦者が次に取る行動が違う。
 */
export const MALFORMED = "malformed";
export type Malformed = typeof MALFORMED;

/**
 * PC 側 PID を持つモータの現在ゲイン。
 *
 * `applies_to` はこの 1 基へ送ったときに実際に適用されるモータ名で、左右直結ペアなら
 * 両方が入る。**名前から対を推測してはならない** — 「1 台だけに効かせてよいか」の
 * 判断はサーバーの `_paired_with()` 1 箇所が持つ。
 */
export interface MotorPid {
  kp: number;
  ki: number;
  kd: number;
  applies_to: string[];
}

export interface MotorState {
  pos: number;
  vel: number;
  torque: number;
  temp: number;
  /**
   * 位置目標。null なら PC 側に目標が無い (PID を持たないモータ・停止中・開ループ)。
   *
   * **0 で埋めてはならない。** 偏差 0 = 完璧に追従している、と読めてしまう。
   * これが配られる前は画面に偏差そのものが存在せず、調整で最も見たい量が
   * 操縦者の頭の中の引き算にしかなかった。
   */
  target: number | null;
  /**
   * 直近周期の出力が出力レンジの端に張り付いたか。
   *
   * 飽和している間はゲインを変えても応答は変わらない。これが見えないと
   * 「kp を上げても下げても同じ」という観察から、制御以外の原因
   * (機構の負荷・config の output_limit) へ辿り着けない。
   */
  saturated: boolean;
  /**
   * null なら PC 側 PID を持たない (ドライバ・ファーム側でループを閉じている)。
   * 調整対象かどうかの判定はこれだけで行い、ドライバ種別を UI へ書き写さない。
   */
  pid: MotorPid | null;
}

export type BusHealthState = "ok" | "degraded" | "down";
export type MotorHealthState = "ok" | "stale" | "warning" | "fault";

export interface BusHealth {
  name: string;
  channel: string;
  state: BusHealthState;
  last_tx_at: EpochSeconds | null;
  last_rx_at: EpochSeconds | null;
  tx_error_count: number;
  rx_error_count: number;
  bus_off: boolean;
  /**
   * 受信の口そのものが読めない状態。bus_off とは原因が別なので相乗りさせない ——
   * bus_off はコントローラがバスから切り離された状態、こちらはインタフェースが
   * down している状態で、復旧の手当ても別になる。
   */
  rx_down: boolean;
}

export interface MotorHealth {
  name: string;
  bus: string;
  state: MotorHealthState;
  last_feedback_at: EpochSeconds | null;
  feedback_age_ms: number | null;
  /** ドライバが温度を返さないモータでは null が来る */
  temperature: number | null;
  detail: string | null;
}

export interface HealthSnapshot {
  timestamp: EpochSeconds;
  overall: BusHealthState;
  buses: BusHealth[];
  motors: MotorHealth[];
  /**
   * 判定できなかった理由。サーバーはヘルス計算そのものが失敗したとき
   * overall=down・buses/motors 空・この detail 付きで「判定不能」を配信する
   * (`lib/server.py` の `_health_unknown`)。内訳が空になる以上、理由はここにしか無い。
   */
  detail: string | null;
}

export type HealthChangeLevel = "info" | "warning" | "critical";

/** ヘルス変化 1 件。受信時刻は UI 側で付ける (`lib/robotReducer.ts`) */
export interface HealthChange {
  robot: string;
  level: HealthChangeLevel;
  target: string;
  from: string;
  to: string;
  message: string;
}

/**
 * 統合動作確認の状態。**両ハンドで 1 つしかない**ので robot を持たない。
 *
 * 進捗も結果も拒否理由も 1 通に載る。かつては progress / record / done / error の
 * 4 種類に分かれており、受け取る側が継ぎ合わせて 1 つの状態を組み立てていた。
 * 途中の 1 通を取りこぼすと画面と機体が食い違ったまま、リロードするまで直らない。
 */
export interface MotorCheckSnapshot {
  /** シーケンスが読み込まれているか。机上ベンチでは false になる */
  available: boolean;
  /**
   * 今この瞬間起動できない理由。押せるなら null。
   *
   * **UI 側で導出し直さないこと。** サーバー (`_motor_check_deny_reason`) が
   * 唯一の判定で、画面は理由を説明するだけ。両者で判定すると、サーバーが
   * 「押せる」と言っているのに画面がボタンを殺す状態が生まれる。
   */
  blocked_reason: string | null;
  running: boolean;
  current_step: string | null;
  step_index: number;
  total_steps: number;
  steps: SequenceStepInfo[];
  /** 直近の拒否・失敗理由。次の起動が成功するまで消えない */
  error: string | null;
}

/**
 * 起動オプション・config 由来の、試合中に変わらない情報。接続直後に 1 度だけ届く。
 *
 * 開発用ボタンの表示可否をビルド時定数で決めると、同じ `web/dist` を配る本番と
 * 開発で再ビルドが要る (= 切り替えとして機能しない)。正はサーバーが持つ。
 *
 * 温度しきい値も同じで、UI 側に定数を持つと config を変えても画面の判定だけが
 * 古い値のまま残り、同じモータについてサーバーと UI が違う答えを出す。
 */
export interface ServerInfo {
  /** 開発用コマンド (指差喚呼の一括チェック等) が解禁されているか */
  dev_tools: boolean;
  /** CAN バス無しで起動しているか (機体は繋がっていない) */
  dry_run: boolean;
  /** モータ温度の警告しきい値 [℃]。未配信は null (UI は色を付けない) */
  temp_warning_c: number | null;
  /** モータ温度の危険しきい値 [℃]。未配信は null (UI は色を付けない) */
  temp_critical_c: number | null;
}

export type MatchCourt = "red" | "blue";
export type MatchPhase = "setup" | "ready" | "match" | "finished";
/**
 * 指差喚呼のロール。サーバーの `lib/match_state.py` の `ALL_ROLES` と 1:1 で対応する。
 *
 * かつては操縦者 2 名 (main_hand / sub_hand) に分かれていたが、2 名が必ず同じ場所で
 * 操縦するため独立した確認にならず、1 つへ統合した。
 */
export type ChecklistRole = "pre_match";

/** 唯一のロール。画面側がロール名の文字列を書かずに済ませるための定数。 */
export const CHECKLIST_ROLE: ChecklistRole = "pre_match";

export interface ChecklistItem {
  id: string;
  label: string;
  checked: boolean;
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
        typeof item.checked === "boolean",
    )
  );
}

/**
 * 指差喚呼の進捗を受信境界で確定させる。未配信は空 (サーバーが古い / 未実装)。
 *
 * **形が違うものを空へ倒してはならない。** `Checklist` は空を「項目が未定義
 * (config/checklist.yaml)」と説明するので、読めなかった配信がそこへ紛れると
 * 操縦者は config を疑って探しに行く。開始可否そのものはサーバーの
 * `can_start_match` が決めるので、ここが判定不能でも試合は始められる。
 */
export function parseChecklists(raw: unknown): Record<string, ChecklistState> | Malformed {
  if (raw === undefined) return {};
  if (!isObject(raw)) return MALFORMED;
  if (!Object.values(raw).every(isChecklistState)) return MALFORMED;
  return raw as Record<string, ChecklistState>;
}

/**
 * 試合時間タイマー。**残り時間ではなく「この配信瞬間の経過ミリ秒」**が載る。
 *
 * 各デバイスはこれを起点に自分の単調時計 (`performance.now()`) で進めるため、
 * デバイス間のずれは WS の片道遅延ぶん (数 ms) に収まり、**端末の壁時計が
 * 揃っている必要がない**。操縦者 2 名 + Monitor が別ブラウザ・別 PC で繋がる
 * 構成では、開始時刻 (エポック秒) を配って各自が引き算する方式は使えない
 * (数秒ずれた 3 つのタイマーが平然と表示され、ずれていることも画面から分からない)。
 *
 * `running` が false のときは進めない。試合終了後はサーバーが終了時点で凍結した
 * 値を送り続けるので、結果確認中に数字が進み続けることがない。
 */
export interface MatchTimer {
  running: boolean;
  /** 試合開始からの経過。サーバーが配信した瞬間の値 */
  elapsed_ms: number;
  /** 試合時間の上限 (config/system.yaml の match.duration_s 由来) */
  duration_ms: number;
}

export interface MatchState {
  court: MatchCourt;
  phase: MatchPhase;
  can_start_match: boolean;
  /**
   * 完了が試合開始のゲートになるロールと、その進捗。キーの集合はサーバーが持つ。
   * 読めなかった配信は `MALFORMED` (空へ倒すと「項目が未定義」と見分けが付かない)。
   */
  checklists: Record<string, ChecklistState> | Malformed;
  /**
   * タイマーが読めなければ null。**match_state ごと捨ててはならない** —
   * フェーズと指差喚呼の進捗は試合の進行そのものを握っており、タイマーが
   * 壊れているという理由でそちらまで落とすほうがはるかに悪い。
   */
  timer: MatchTimer | null;
}

export interface SequenceStepInfo {
  index: number;
  label: string;
  require_trigger: boolean;
}

/** 位置制御ループ 1 本 (= 同一バス上の M3508 を束ねる 200Hz ループ) の状態 */
export interface PositionLoopState {
  bus: string;
  running: boolean;
  paused: boolean;
  sync_violations: string[];
}

/** 同期監視 1 本 (50Hz) の状態 */
export interface SyncMonitorState {
  axes: string[];
  running: boolean;
  violated: string[];
}

/**
 * 目標値再送タスク 1 本 (= 自作モータドライバ向け 20Hz の再送) の状態。
 *
 * ファーム側は 500ms のコマンドウォッチドッグを持つため、これが止まると
 * 500ms 後に generic アクチュエータ (グリッパ・コンベア・壁) が停止する。
 */
export interface TargetRefresherState {
  motors: string[];
  running: boolean;
  paused: boolean;
}

/**
 * 安全機構の状態。
 *
 * `sync_violations` が空でない軸は左右のずれを検知してラッチされており、
 * 緊急停止を解除しても動かない (機構を直して解除し直す必要がある)。
 * `loops_running` / `monitors_running` / `refreshers_running` が false なら
 * 200Hz の位置制御ループ・50Hz の同期監視・20Hz の目標値再送のいずれかが死んでいる。
 * WS は繋がったままモータ状態も届き続けるため、ここを読まない限り誰も気付けない。
 */
export interface SafetyState {
  sync_violations: string[];
  /**
   * 励磁されているべきなのに無励磁のモータ。
   *
   * 緊急停止を解除して有効化を試みた後も残っているものだけが載る。**この異常は
   * 他のどこにも現れない** —— フィードバックは正常に届き、ヘルスは OK、CAN の
   * カウンタも平常で、操縦者から見えるのは「指令しても動かない」だけになる。
   * 励磁状態を報告しないドライバ (自作モタドラ・C620) は最初から対象外。
   */
  unenergized_motors: string[];
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

/**
 * 周期タスク 1 本ぶんの検査。**UI が実際に読む欄しか見ない** ——
 * `paused` や `violated` は契約上 `unused` と宣言してあり、欠けても表示は成立する。
 * 読まない欄まで必須にすると、判定不能へ倒す理由が「表示に影響しない欠落」で埋まる。
 */
const SAFETY_TASK_SHAPES: Record<string, (task: Raw) => boolean> = {
  position_loops: (t) => typeof t.bus === "string" && typeof t.running === "boolean",
  sync_monitors: (t) => isStringArray(t.axes) && typeof t.running === "boolean",
  target_refreshers: (t) => isStringArray(t.motors) && typeof t.running === "boolean",
};

/**
 * `SafetyState` として読めない欄を挙げる (空なら読める)。
 *
 * 型は実行時に消えるので、`safety.sync_violations.length` のような読み方は
 * 配信から 1 欄落ちただけで例外になる。しかも呼び出し元はどれもレンダー本体なので、
 * 投げれば React ツリーごとアンマウントし、ヘッダーの緊急停止ボタンまで消える。
 */
export function safetyShapeErrors(value: unknown): string[] {
  if (!isObject(value)) return ["safety"];

  const broken: string[] = [];
  for (const key of ["sync_violations", "unenergized_motors"]) {
    if (!isStringArray(value[key])) broken.push(key);
  }
  for (const key of ["loops_running", "monitors_running", "refreshers_running"]) {
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

/**
 * 安全機構ペイロードを受信境界で確定させる。
 *
 * 検証を通ったら**配信オブジェクトをそのまま返す** (組み立て直さない)。`paused` の
 * ような UI が読まない欄まで写し取る責務をここに持たせると、サーバーが欄を足すたびに
 * ここが取りこぼす側になる。
 */
export function parseSafety(raw: unknown): SafetyState | Malformed | undefined {
  if (raw === undefined) return undefined;
  return safetyShapeErrors(raw).length === 0 ? (raw as SafetyState) : MALFORMED;
}

/**
 * 操作モード。**モータの制御モード (position / velocity / duty) とは別物。**
 * あちらはモータへ送る指令の種類、こちらは「制御権を誰が握っているか」。
 */
export type OperationMode = "sequence" | "manual";

/**
 * 手動操縦で連続値を送ってよい範囲とジョグ量の候補 (位置定数 yaml の `axes.<軸>.manual`)。
 *
 * これを持たない軸は連続操作の対象外で、位置名によるプリセット指令だけができる。
 * 通常運用の `move_to` は位置名でしか値を引けず「定義した状態以外を送れない」ことが
 * 構造的に保証されているので、その保証を外す手動には代わりの境界が要る。
 */
export interface ManualRange {
  min: number;
  max: number;
  /** UI が出すジョグ量の候補。先頭が既定値。空にはならない */
  steps: number[];
}

export interface ManualAxis {
  name: string;
  /** 人間が扱う単位 (mm / deg / duty)。表示にそのまま使う */
  unit: string;
  command_mode: "position" | "velocity" | "duty";
  /**
   * フィードバックから逆換算した現在値。**位置を測れない軸では null。**
   * DC 基板はエンコーダを持たないので、0 を載せると「測ったように見える 0」になる。
   * 数値へフォールバックせず「読めていない」ことを画面に出すこと。
   */
  value: number | null;
  /** 直前に手動で送った目標値。一度も送っていなければ null */
  target: number | null;
  /** 連続操作を許した軸だけが持つ。null ならプリセット指令のみ */
  manual: ManualRange | null;
  /**
   * 左右直結ペアの現在のずれ (軸の単位)。**ずれようのない軸と測れない軸は null。**
   *
   * サーバーが 3 層の保護と同じ `SyncGroup.deviation()` で算出した値をそのまま配る。
   * UI 側で `motors` の位置から計算し直してはならない —— 逆回転ペアは scale の符号で
   * 表されており、符号を 1 つ落とすと画面だけが別の「ずれ」を言い出す。
   *
   * **0.0 は正常な値であって欠落ではない** (揃っている状態)。falsy 判定で捨てないこと。
   */
  deviation: number | null;
  /**
   * 偏差の許容差 (軸の単位)。`config` の `sync_tolerance` が唯一の正で、
   * UI はフォールバック値を持たない。null なら色を付けず数値も判定しない。
   */
  sync_tolerance: number | null;
  /** 位置定数に定義された状態名。プリセットボタンはここからしか作らない */
  positions: string[];
  motors: string[];
}

export interface ManualState {
  mode: OperationMode;
  axes: ManualAxis[];
}

export interface RobotState {
  type?: "state";
  robot: string;
  sequence: string;
  /**
   * 現在ステップ名。画面では `steps[step_index].label` を使うため描画には使わないが、
   * サーバーが配信し続けている値なので契約として型に残す (契約テストが存在を守る)。
   */
  current_step: string | null;
  step_index: number;
  total_steps: number;
  waiting_trigger: boolean;
  /**
   * シーケンス実行中フラグ。**step_index / total_steps から推測しないこと。**
   * 以前 `step_index === 0 && total_steps > 0` を「未実行」の代用にしたところ、
   * 準備フェーズでは常に成立して動作確認ボタンが常時無効になった。
   */
  running?: boolean;
  motors: Record<string, MotorState>;
  e_stop_active?: boolean;
  health?: HealthSnapshot;
  /**
   * 安全機構。受信境界の `parseSafety` が形を確定させるので、読めなかった配信は
   * `MALFORMED` としてここに載る (**空の SafetyState へ倒さない** — 平常時と
   * 見分けが付かなくなる)。未配信は undefined。
   */
  safety?: SafetyState | Malformed;
  steps?: SequenceStepInfo[];
  /**
   * 操作モードと手動操縦の軸一覧。
   *
   * 軸定義 (可動範囲・プリセット名) は静的だが `steps` と同じく state に載っている。
   * **軸名も可動範囲も UI 側へ書かないこと** — 機構が変わって軸が増減しても
   * UI の変更が要らない性質は、ここをそのまま描くことで成り立っている。
   */
  manual?: ManualState;
}

/** 受信条件を通ったメッセージ。UI 状態へ入れる形まで正規化してある */
/** 助言 1 件の重み。色分けはこれだけで決める */
export type AdviceSeverity = "ok" | "info" | "warning";

export interface TuningAdvice {
  code: string;
  severity: AdviceSeverity;
  message: string;
}

/**
 * ステップ応答から読み取れた指標。
 *
 * **測れなかった項目は null。0 で埋めてはならない** — 行き過ぎが無かった応答と
 * 窓の中で目標へ届かなかった応答が同じ表示になり、次に取るべき行動が正反対になる。
 */
export interface TuningMetrics {
  step_from: number;
  step_to: number;
  step_size: number;
  rise_time_s: number | null;
  overshoot_pct: number;
  peak_time_s: number | null;
  settling_time_s: number | null;
  steady_state_error: number;
  oscillation_hz: number | null;
  damping_ratio: number | null;
  saturation_ratio: number;
  peak_output: number;
  settle_band: number;
  sample_count: number;
  duration_s: number;
}

/** 測れなかったときだけ null になる欄。それ以外の null は配信の欠落 */
const NULLABLE_METRICS = [
  "rise_time_s",
  "peak_time_s",
  "settling_time_s",
  "oscillation_hz",
  "damping_ratio",
] as const;

const REQUIRED_METRICS = [
  "step_from",
  "step_to",
  "step_size",
  "overshoot_pct",
  "steady_state_error",
  "saturation_ratio",
  "peak_output",
  "settle_band",
  "sample_count",
  "duration_s",
] as const;

/**
 * 指標を受信境界で確定させる。
 *
 * `isObject` だけを条件にしていた頃は、半端な形がそのまま `MetricsPanel` へ渡り
 * `m.overshoot_pct.toFixed(0)` がレンダー本体で投げていた。**測れなかった項目の
 * null (`rise_time_s` 等) と、配信が欠けたことを混ぜてはならない** — 前者は
 * 「—」と出すのが正しく、後者は指標そのものを信用してはいけない。
 */
export function parseTuningMetrics(raw: unknown): TuningMetrics | Malformed | null {
  // null は「ステップとして解釈できなかった」の表現であって欠落ではない
  if (raw === null || raw === undefined) return null;
  if (!isObject(raw)) return MALFORMED;
  const isNum = (v: unknown) => typeof v === "number" && Number.isFinite(v);
  if (!REQUIRED_METRICS.every((key) => isNum(raw[key]))) return MALFORMED;
  if (!NULLABLE_METRICS.every((key) => raw[key] === null || isNum(raw[key]))) return MALFORMED;
  return raw as unknown as TuningMetrics;
}

/**
 * 描画に使える指標だけを取り出す。未解釈 (null) も判定不能 (MALFORMED) も null。
 *
 * **どちらも「数字を出してはいけない」点では同じ**なので、グラフの整定帯のように
 * 値そのものを使う箇所はこれを通す。両者を言い分ける必要があるのは、操縦者へ
 * 理由を説明する 1 箇所 (`ResponsePanel`) だけ。
 */
export function readableMetrics(metrics: TuningMetrics | Malformed | null): TuningMetrics | null {
  return metrics === null || metrics === MALFORMED ? null : metrics;
}

/** 波形。点ごとのオブジェクトではなく列で運ぶ (同じキー名の繰り返しを避ける) */
export interface TuningSamples {
  t: number[];
  target: number[];
  pos: number[];
  output: number[];
  sat: boolean[];
}

/**
 * 1 回のステップ応答。波形・指標・助言を 1 通で運ぶ。
 *
 * 分けて配ると、波形だけ届いて指標が来ていない画面や、指標が新しく波形が古い
 * 画面が作れてしまう。調整はこの 3 つを突き合わせる作業なので、途中の 1 通を
 * 落とした画面はそのまま誤読につながる。
 */
export interface TuningCapture {
  robot: string;
  motor: string;
  captured_at: number;
  gains: { kp: number; ki: number; kd: number };
  /**
   * ステップとして解釈できなかった記録では null (助言も空になる)。
   * 読めなかった配信は `MALFORMED` — null へ倒すと「解釈できなかった」と混ざる。
   */
  metrics: TuningMetrics | Malformed | null;
  advice: TuningAdvice[];
  samples: TuningSamples;
}

export type ServerMessage =
  | { type: "state"; robot: string; state: RobotState }
  | { type: "server_info"; serverInfo: ServerInfo }
  | { type: "match_state"; matchState: MatchState }
  | { type: "e_stop_state"; active: boolean; reason: string | null }
  | { type: "command_rejected"; command: string; reason: string }
  | { type: "health_change"; event: HealthChange }
  | { type: "motor_check_state"; motorCheck: MotorCheckSnapshot }
  | { type: "tuning_capture"; capture: TuningCapture };

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

/**
 * タイマーを読む。3 値が揃っていなければ null。
 *
 * `duration_ms <= 0` を通すと残り時間が常に 0 以下になり、画面には
 * 「試合開始と同時に時間切れ」が出る。値の欠落として扱い、表示側に
 * 「読めていない」ことを出させる (誤った数字を自信満々に出すより良い)。
 */
function parseTimer(raw: unknown): MatchTimer | null {
  if (!isObject(raw)) return null;
  if (typeof raw.running !== "boolean") return null;
  if (typeof raw.elapsed_ms !== "number" || !Number.isFinite(raw.elapsed_ms)) return null;
  if (typeof raw.duration_ms !== "number" || !Number.isFinite(raw.duration_ms)) return null;
  if (raw.duration_ms <= 0) return null;
  return { running: raw.running, elapsed_ms: raw.elapsed_ms, duration_ms: raw.duration_ms };
}

/** どのロボットの話か決められないメッセージは捨てるしかない */
function robotOf(raw: Raw): string | null {
  return typeof raw.robot === "string" && raw.robot.length > 0 ? raw.robot : null;
}

/**
 * 波形の列を読む。**列の長さが揃っていなければ null。**
 *
 * 揃っていない列をそのまま描くと、`t` の長さでループした先で `pos` が
 * undefined になり、グラフだけが静かに途切れる (例外は出ない)。長さの
 * 食い違いは配信側の不具合であって、部分的に描いてよい状態ではない。
 */
function parseTuningSamples(raw: unknown): TuningSamples | null {
  if (!isObject(raw)) return null;
  const t = raw.t;
  const target = raw.target;
  const pos = raw.pos;
  const output = raw.output;
  const sat = raw.sat;
  if (
    !Array.isArray(t) ||
    !Array.isArray(target) ||
    !Array.isArray(pos) ||
    !Array.isArray(output) ||
    !Array.isArray(sat)
  ) {
    return null;
  }
  const lengths = new Set([t.length, target.length, pos.length, output.length, sat.length]);
  if (lengths.size !== 1) return null;
  return {
    t: t as number[],
    target: target as number[],
    pos: pos as number[],
    output: output as number[],
    sat: sat as boolean[],
  };
}

function parseKnown(raw: Raw): ServerMessage | null {
  const robot = robotOf(raw);

  switch (raw.type) {
    case "state": {
      if (robot === null) return null;
      // **`motors` と `steps` は素通しのまま。** モータ名をハードコードしない性質は
      // 配信をそのまま UI 状態へ入れることで成り立っており、ここで組み立て直すと
      // モータが 1 基増えるたびに UI 側の変更が要る形へ逆戻りする。
      // 形を確定させるのは、UI が `.length` / `.filter` を直に呼ぶ `safety` だけ
      const state = { ...(raw as unknown as RobotState) };
      const safety = parseSafety(raw.safety);
      if (safety !== undefined) state.safety = safety;
      return { type: "state", robot, state };
    }

    case "server_info":
      // 欠けたフラグは「無効」に倒す。開発用ボタンが本番で出るより出ない方が安全。
      // しきい値も同じで、number でなければ null にして「判定しない」へ倒す
      // (代わりの既定値を UI が持つと、それがそのまま二重管理になる)
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
      // サーバーが正。接続直後のスナップショットと変化通知の両方がここに来る
      return {
        type: "match_state",
        matchState: {
          court: raw.court as MatchCourt,
          phase: raw.phase as MatchPhase,
          can_start_match: Boolean(raw.can_start_match),
          checklists: parseChecklists(raw.checklists),
          timer: parseTimer(raw.timer),
        },
      };

    case "e_stop_state":
      if (typeof raw.active !== "boolean") return null;
      // 試合中になぜ止まったか (操縦者が押したのか SyncMonitor が発報したのか) が
      // 分からないと復旧手順を選べない。サーバーは理由を載せて配信している
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
          level: (raw.level as HealthChangeLevel) ?? "info",
          target: str(raw.target),
          from: str(raw.from),
          to: str(raw.to),
          message: str(raw.message),
        },
      };

    case "motor_check_state":
      // **robot を要求しない。** 両ハンド統合の 1 本なので載っていない。
      // ここで robot を必須にすると、動作確認の状態が 100% 捨てられる
      // (health_change で実際にやらかした形)
      return {
        type: "motor_check_state",
        motorCheck: {
          available: raw.available === true,
          blocked_reason: typeof raw.blocked_reason === "string" ? raw.blocked_reason : null,
          running: raw.running === true,
          current_step: typeof raw.current_step === "string" ? raw.current_step : null,
          step_index: num(raw.step_index),
          total_steps: num(raw.total_steps),
          steps: Array.isArray(raw.steps) ? (raw.steps as SequenceStepInfo[]) : [],
          error: typeof raw.error === "string" ? raw.error : null,
        },
      };

    case "tuning_capture": {
      if (robot === null) return null;
      const samples = parseTuningSamples(raw.samples);
      // 波形が読めない記録は捨てる。指標だけ出しても、操縦者はその数字が
      // どの動きから出たのかを確かめる手段を失う
      if (samples === null) return null;
      return {
        type: "tuning_capture",
        capture: {
          robot,
          motor: str(raw.motor),
          captured_at: num(raw.captured_at),
          gains: {
            kp: num((raw.gains as Raw | undefined)?.kp),
            ki: num((raw.gains as Raw | undefined)?.ki),
            kd: num((raw.gains as Raw | undefined)?.kd),
          },
          // metrics が null なのは「ステップとして解釈できなかった」の表現。
          // 読めない形は MALFORMED として区別する (null へ倒すと、指標を出せなかった
          // 記録と欠けた配信が同じ表示になり、配信側の不具合が誰にも見えない)
          metrics: parseTuningMetrics(raw.metrics),
          advice: Array.isArray(raw.advice) ? (raw.advice as TuningAdvice[]) : [],
          samples,
        },
      };
    }

    default:
      // 未知の type は無視する。サーバーが送り始めたものを取りこぼしていないかは
      // 契約テスト (test/wsContract.test.ts) が実配信サンプルで検出する
      return null;
  }
}

/** 受信フレーム 1 通を解釈する。壊れた JSON・受信条件を満たさないものは null */
export function parseServerMessage(data: string): ServerMessage | null {
  let raw: unknown;
  try {
    raw = JSON.parse(data);
  } catch {
    return null;
  }
  return isObject(raw) ? parseKnown(raw) : null;
}
