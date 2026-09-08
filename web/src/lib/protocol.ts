import type { EpochSeconds } from "@/lib/time";

/**
 * サーバー (`lib/server.py`) が WebSocket で配信するメッセージの型と受信条件。
 *
 * 設計の理由は docs/invariants.md 「WS のワイヤ型と受信条件は
 * `web/src/lib/protocol.ts` にしかない」「受信境界では『読めなかった配信』を
 * `MALFORMED` として異常側へ倒す」。ここは最下層なので `hooks/` を import しない。
 * 受信条件を厳しくするときは必ず `test/wsContract.test.ts` で実配信を確認すること
 * (型が合っていても条件が弾けば画面には何も出ない)。
 */

/**
 * 受信条件を満たさなかったペイロードの印。表示側は異常側として扱う。
 * 既定値で埋めてはならない理由と、未配信 (undefined) と区別する理由は
 * docs/invariants.md 「受信境界では『読めなかった配信』を `MALFORMED` として
 * 異常側へ倒す」。
 */
export const MALFORMED = "malformed";
export type Malformed = typeof MALFORMED;

/**
 * テレメトリの測定値 1 つ。**`null` は「そのドライバに測る手段が無い」という正当な
 * 測定結果**であって、配信が読めなかったことではない (DC 基板・電磁弁基板は
 * エンコーダも電流センスも温度センサも持たない)。両者を分ける唯一の入口が
 * `readMeasured()`。docs/invariants.md 「測れない項目は配信の境界で `null` へ倒す」。
 */
export type Measured = number | null;

/**
 * 測定値を表示境界で確定させる。数値はそのまま / `null` は `—` / 欠落・型違いは
 * `MALFORMED`。
 *
 * **`motors` は受信境界 (`parseKnown`) では素通しのまま**にしてあり (モータ名を
 * UI へ書かない性質がそれで成立している)、代わりに数値を実際に読む側がここを通す。
 * 型は実行時に消えるので `state.pos.toFixed(1)` は欄が 1 つ落ちただけでレンダー本体
 * から TypeError が飛び、React ツリーごとアンマウントする。
 */
export function readMeasured(value: unknown): Measured | Malformed {
  if (value === null) return null;
  return typeof value === "number" && Number.isFinite(value) ? value : MALFORMED;
}

/**
 * 指令値を表示境界で確定させる。`readMeasured` との違いは **未配信 (undefined) を
 * 異常にしない**ことだけ —— `command` は後から足された欄なので、配らない版の
 * サーバーへ繋ぐと全モータの POS 欄が `?` で埋まる。型違いだけを異常にする。
 */
export function readCommand(value: unknown): Measured | Malformed {
  return value === undefined ? null : readMeasured(value);
}

export interface MotorState {
  /** 測る手段が無いドライバでは `null`。可否はサーバーが判定して配る */
  pos: Measured;
  vel: Measured;
  torque: Measured;
  temp: Measured;
  /**
   * PC が最後にそのモータへ送った目標値。一度も指令していなければ null。
   *
   * **指令値であって実出力ではない。** ファーム側の `max_duty` クランプ (既定 0.30) /
   * `everFed_` ゲート / コマンドウォッチドッグ満了 (500ms) / 緊急停止ラッチと基板の
   * 再起動 のどれでも食い違う。**基板が止まっていてもここには値が載り続ける。**
   */
  command: Measured;
  /**
   * `command` の指令種別 (`position` / `duty` / `on_off` 等)。指令が無ければ null。
   * 表示の丸め方と単位はこれだけで決める (モータ名や基板の種類から推測しない)。
   */
  command_mode: string | null;
}

/**
 * ヘルスの語彙。**実行時の集合と型を 1 つの宣言から作る** —— 集合を型と別に
 * 書き写すと「型には無いのに検査は通る」値が生まれる。
 */
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
  /**
   * 受信の口そのものが読めない状態。bus_off (コントローラがバスから切り離された)
   * とは原因も復旧の手当ても別なので相乗りさせない。
   */
  rx_down: boolean;
  /**
   * 途絶の「立ち上がり」を数えた累積回数。bus-off 復旧の down/up (1 秒弱) のような
   * 一過性の途絶は `rx_down` では画面に一瞬しか出ず、機体を見ている操縦者は見落とす。
   * 復帰しても 0 に戻らない (サーバーが試合開始でリセットする)。
   */
  rx_down_episodes: number;
  /**
   * このバスの途絶がワーク落下に繋がりうるか。**判定はサーバーだけが行う** ——
   * バス名やドライバ種別を UI へ書き写すと、弁のバスを config で変えた瞬間に
   * 判定が古いまま残る。
   */
  may_affect_workpiece: boolean;
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
   * 判定できなかった理由 (`lib/server.py` の `_health_unknown`)。内訳が空になる以上、
   * 理由はここにしか無い。
   */
  detail: string | null;
}

/** `name` と既知の `state` を持つ配列か。ヘルスの内訳 (buses / motors) 共通の形 */
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

/**
 * `HealthSnapshot` として読めない欄を挙げる (空なら読める)。
 *
 * **UI が実際に読む欄しか見ない** (`safetyShapeErrors` と同じ方針)。`evaluateHealth` は
 * 内訳を無検査で `filter` するので、配列でないだけでレンダー本体から TypeError が
 * 飛ぶ。しかも呼び出し元の 1 つ (`TabBar`) は `RouteErrorBoundary` の**外**にあるため、
 * **ヘッダーの緊急停止ボタンまで画面から消える**。
 */
export function healthShapeErrors(value: unknown): string[] {
  if (!isObject(value)) return ["health"];

  const broken: string[] = [];
  if (typeof value.overall !== "string" || !BUS_HEALTH_STATES.includes(value.overall as never)) {
    broken.push("overall");
  }
  if (!isHealthEntryArray(value.buses, BUS_HEALTH_STATES)) broken.push("buses");
  if (!isHealthEntryArray(value.motors, MOTOR_HEALTH_STATES)) broken.push("motors");
  // 判定不能の理由はここにしか無く、そのまま画面へ文字として出る
  if (value.detail !== null && value.detail !== undefined && typeof value.detail !== "string") {
    broken.push("detail");
  }
  return broken;
}

/**
 * ヘルスを受信境界で確定させる。未配信は undefined、読めない形は `MALFORMED`。
 * **空の `HealthSnapshot` へ倒してはならない** —— 内訳が空の判定は「ヘルス未取得」
 * (色を付けない) になり、読めなかったことが画面から消える。
 */
export function parseHealth(raw: unknown): HealthSnapshot | Malformed | undefined {
  if (raw === undefined) return undefined;
  return healthShapeErrors(raw).length === 0 ? (raw as HealthSnapshot) : MALFORMED;
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
 * 統合動作確認の状態。**両ハンドで 1 つしかない**ので robot を持たない。進捗も結果も
 * 拒否理由も 1 通に載る —— 種類を分けると受け取る側が継ぎ合わせることになり、途中の
 * 1 通を取りこぼすと画面と機体が食い違ったままリロードまで直らない。
 */
export interface MotorCheckSnapshot {
  /** シーケンスが読み込まれているか。机上ベンチでは false になる */
  available: boolean;
  /**
   * 今この瞬間起動できない理由。押せるなら null。**UI 側で導出し直さないこと** ——
   * サーバー (`_motor_check_deny_reason`) が唯一の判定で、画面は理由を説明するだけ。
   */
  blocked_reason: string | null;
  running: boolean;
  current_step: string | null;
  step_index: number;
  total_steps: number;
  /** ステップ一覧。読めない配信は `MALFORMED` (理由は `parseMotorCheckSteps`) */
  steps: SequenceStepInfo[] | Malformed;
  /** 直近の拒否・失敗理由。次の起動が成功するまで消えない */
  error: string | null;
  /**
   * どのステップで失敗したか。平常時は null。`error` (表示 1 行) と同じ失敗なので
   * **表示は 1 つに畳む** (`lib/motorCheckStatus.ts` が唯一の畳み先)。
   */
  last_error: SequenceFailure | null;
  /**
   * 構成に無い軸を指令するため登録されなかったステップ。除外が無ければ空配列。
   * 読めない配信は `MALFORMED` (理由は `parseExcludedSteps`)。
   */
  excluded_steps: ExcludedStep[] | Malformed;
}

/**
 * 起動オプション・config 由来の、試合中に変わらない情報。接続直後に 1 度だけ届く。
 * 開発用ボタンの表示可否をビルド時定数で決めると、同じ `web/dist` を配る本番と開発で
 * 再ビルドが要る (= 切り替えとして機能しない)。温度しきい値の正が config だけである
 * 理由は docs/invariants.md 「UI はしきい値のフォールバック値を持たない」。
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

/**
 * コートとフェーズの語彙。**実行時の集合と型を 1 つの宣言から作る。**
 *
 * どちらも `Record` の索引として使われるので、未知の値が素通しで入ると索引が
 * `undefined` になり**チップが無地・無文字で消える**。フェーズはさらに
 * `isDuringMatch()` を false にして画面全体を「準備中」へ倒す。
 */
export const MATCH_COURTS = ["red", "blue"] as const;
export type MatchCourt = (typeof MATCH_COURTS)[number];

export const MATCH_PHASES = ["setup", "ready", "match", "finished"] as const;
export type MatchPhase = (typeof MATCH_PHASES)[number];
/** 指差喚呼のロール。サーバーの `lib/match_state.py` の `ALL_ROLES` と 1:1 で対応する */
export type ChecklistRole = "pre_match";

/** 唯一のロール。画面側がロール名の文字列を書かずに済ませるための定数。 */
export const CHECKLIST_ROLE: ChecklistRole = "pre_match";

export interface ChecklistItem {
  id: string;
  label: string;
  checked: boolean;
  /**
   * 画面上でどのコントロールの隣に置くかの宣言 (`config/checklist.yaml` の `group`)。
   *
   * **未指定・未知の名前でも項目を落としてはならない** (語彙と配置の対応は
   * `lib/checklistGroups.ts` が持ち、そこに無い group は「その他」として描く)。
   * 既知の値へ型を狭めないのは、UI の型が config の語彙より遅れたときに「配信には
   * 居るのに画面から消えた項目」を作らないため。
   */
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
        // group は省略可 (区分を持たない設定がある)。文字列以外が載っていたら配信
        // そのものを疑う —— 黙って「その他」へ倒すと、配置だけが効かない状態が
        // 画面のどこにも現れない
        (item.group === undefined || item.group === null || typeof item.group === "string"),
    )
  );
}

/**
 * 指差喚呼の進捗を受信境界で確定させる。未配信は空 (サーバーが古い / 未実装)。
 *
 * **形が違うものを空へ倒してはならない** —— `Checklist` は空を「項目が未定義
 * (config/checklist.yaml)」と説明するので、読めなかった配信がそこへ紛れると操縦者は
 * config を疑って探しに行く。開始可否はサーバーの `can_start_match` が決めるので、
 * ここが判定不能でも試合は始められる。
 */
export function parseChecklists(raw: unknown): Record<string, ChecklistState> | Malformed {
  if (raw === undefined) return {};
  if (!isObject(raw)) return MALFORMED;
  if (!Object.values(raw).every(isChecklistState)) return MALFORMED;
  return raw as Record<string, ChecklistState>;
}

/**
 * 試合時間タイマー。**残り時間ではなく「この配信瞬間の経過ミリ秒」**が載る
 * (理由は docs/invariants.md 「試合時間タイマーは『時刻』ではなく『配信瞬間の
 * 経過ミリ秒』を配る」)。各デバイスはこれを起点に `performance.now()` で進める。
 *
 * `running` が false のときは進めない。試合終了後はサーバーが終了時点で凍結した値を
 * 送り続けるので、結果確認中に数字が進み続けることがない。
 */
export interface MatchTimer {
  running: boolean;
  /** 試合開始からの経過。サーバーが配信した瞬間の値 */
  elapsed_ms: number;
  /** 試合時間の上限 (config/system.yaml の match.duration_s 由来) */
  duration_ms: number;
}

export interface MatchState {
  /**
   * 既知値でなければ `MALFORMED`。**適当な既定 (`red`) へ倒してはならない** ——
   * コートは誤設定のまま試合に入る事故を防ぐために常時表示している要素で、
   * 埋めた瞬間に「読めていない」ことが画面から消える。
   */
  court: MatchCourt | Malformed;
  /** 既知値でなければ `MALFORMED`。倒す先が無いので「フェーズ不明」として見せる */
  phase: MatchPhase | Malformed;
  can_start_match: boolean;
  /**
   * 完了が試合開始のゲートになるロールと、その進捗。キーの集合はサーバーが持つ。
   * 読めなかった配信は `MALFORMED` (空へ倒すと「項目が未定義」と見分けが付かない)。
   */
  checklists: Record<string, ChecklistState> | Malformed;
  /**
   * タイマーが読めなければ null。**match_state ごと捨ててはならない** —— フェーズと
   * 指差喚呼の進捗は試合の進行そのものを握っており、タイマーが壊れているという理由で
   * そちらまで落とすほうがはるかに悪い。
   */
  timer: MatchTimer | null;
}

export interface SequenceStepInfo {
  index: number;
  label: string;
  require_trigger: boolean;
}

/**
 * 直近の実行で失敗したステップと理由 (サーバー `lib/sequence/engine.py` の `StepFailure`)。
 *
 * 到達タイムアウト・左右ずれ・零点確定失敗はどれもステップ単位の try で握られるので、
 * これが無いと画面は「待機中」へ戻り **3 層保護の第 1 層 (`AxisSyncError`) が操縦者から
 * 無音になる。** 次の実行が始まるまで保持される。
 */
export interface SequenceFailure {
  step_index: number;
  /** 失敗したステップのラベル。メソッド名は載らない */
  step: string;
  message: string;
}

/**
 * 失敗理由を受信境界で確定させる。**null と欠落を同じ「出すものが無い」へ倒す。**
 * 3 欄すべてが揃っていなければ表示しない —— 半端な形を渡すと `step_index + 1` が
 * `NaN` になった行が「ステップ NaN で停止」として画面に出る。
 */
export function parseSequenceFailure(raw: unknown): SequenceFailure | null {
  if (!isObject(raw)) return null;
  if (typeof raw.step_index !== "number" || !Number.isFinite(raw.step_index)) return null;
  if (typeof raw.step !== "string") return null;
  if (typeof raw.message !== "string" || raw.message.length === 0) return null;
  return { step_index: raw.step_index, step: raw.step, message: raw.message };
}

/**
 * 構成に無い軸を指令するため登録されなかったステップ
 * (サーバー `lib/sequence/engine.py` の `ExcludedStep`)。
 *
 * **欠けている軸まで出す**ので、操縦者は「機構が未装着だから減っている」のか
 * 「書き忘れで減っている」のかを画面で区別できる。
 */
export interface ExcludedStep {
  /** 除外されたステップのラベル */
  step: string;
  /** そのステップが指令するはずで、構成に存在しない軸 */
  missing_axes: string[];
}

/**
 * 除外ステップを受信境界で確定させる。**読めない形も欠落も `MALFORMED`** ——
 * 空配列は「除外なし = 全ステップが登録されている」を意味するので、`?? []` で埋めると
 * 除外が起きているのに画面は平常を描く。
 */
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

/**
 * 動作確認のステップ一覧を読む。読めなければ `MALFORMED` —— 空配列は「まだ読み込まれて
 * いない」という**別の意味**を既に持っており、`?? []` へ倒すと指差喚呼「アクチュエータ
 * 動作確認 完了」の判断材料が静かに嘘になる。要素の形まで見るのは `step.index` を
 * レンダー本体で読むため (1 要素でも `null` が混ざると TypeError で落ちる)。
 */
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
 * これが止まると 500ms のコマンドウォッチドッグ満了で generic アクチュエータ
 * (グリッパ・コンベア・壁) が停止する。
 */
export interface TargetRefresherState {
  motors: string[];
  running: boolean;
  paused: boolean;
}

/**
 * 安全機構の状態。`sync_violations` が空でない軸はラッチされており、緊急停止を解除しても
 * 動かない。`loops_running` / `monitors_running` / `refreshers_running` が false なら
 * 200Hz の位置制御ループ・50Hz の同期監視・20Hz の目標値再送のいずれかが死んでいる ——
 * WS は繋がったままモータ状態も届き続けるため、ここを読まない限り誰も気付けない。
 */
export interface SafetyState {
  sync_violations: string[];
  /**
   * 励磁されているべきなのに無励磁のモータ (解除後の有効化を試みても残ったもの)。
   * **この異常は他のどこにも現れない** —— フィードバックもヘルスも CAN のカウンタも
   * 平常で、見えるのは「指令しても動かない」だけになる。励磁状態を報告しないドライバ
   * (自作モタドラ・C620) は対象外。
   */
  unenergized_motors: string[];
  /**
   * 起動の猶予を過ぎても自己申告 (`INFO`) を一度も受けていない自作モタドラ。
   * **これは「壊れている」ではなく「焼き忘れ検出 (`info_mismatch`) が働いていない」の
   * 報告**なので `evaluateHealth()` の判定 (tone) はここでは動かさない。詳細は
   * docs/invariants.md 「送信バッファの本数は基板ごとに違う」。
   */
  firmware_unconfirmed_motors: string[];
  /**
   * 投げっぱなしタスク (`RobotServer.watch_task`) が失敗したときの人が読めるラベル一覧。
   * 平常時は空配列。**古い順に並び、上限超過分は古いものから消える。復帰しても消えない**
   * —— リセットは試合開始の前縁リセットだけ。
   */
  failed_tasks: string[];
  /**
   * モータの励磁し直しがサーバー側で処理中か。単発の再励磁 (`reenergize_motors`) と
   * 緊急停止解除の再励磁の**どちらでも**立つ。押してから 0.1〜1.5 秒のあいだ
   * `unenergized_motors` は消えないので、これが無いと操縦者は「押しても何も起きない」と
   * しか読めず 2 回目を押す。**可否の判定を UI 側で組み立て直さない。**
   */
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

/**
 * 周期タスク 1 本ぶんの検査。**UI が実際に読む欄しか見ない** —— `paused` や
 * `violated` は契約上 `unused` と宣言してあり、欠けても表示は成立する。読まない欄まで
 * 必須にすると、判定不能へ倒す理由が「表示に影響しない欠落」で埋まる。
 */
const SAFETY_TASK_SHAPES: Record<string, (task: Raw) => boolean> = {
  position_loops: (t) => typeof t.bus === "string" && typeof t.running === "boolean",
  sync_monitors: (t) => isStringArray(t.axes) && typeof t.running === "boolean",
  target_refreshers: (t) => isStringArray(t.motors) && typeof t.running === "boolean",
};

/**
 * `SafetyState` として読めない欄を挙げる (空なら読める)。
 *
 * 型は実行時に消えるので `safety.sync_violations.length` は配信から 1 欄落ちただけで
 * 例外になる。しかも呼び出し元はどれもレンダー本体なので、投げれば React ツリーごと
 * アンマウントし、ヘッダーの緊急停止ボタンまで消える。
 */
export function safetyShapeErrors(value: unknown): string[] {
  if (!isObject(value)) return ["safety"];

  const broken: string[] = [];
  for (const key of [
    "sync_violations",
    "unenergized_motors",
    "firmware_unconfirmed_motors",
    "failed_tasks",
  ]) {
    if (!isStringArray(value[key])) broken.push(key);
  }
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

/**
 * 安全機構ペイロードを受信境界で確定させる。検証を通ったら**配信オブジェクトを
 * そのまま返す** (組み立て直さない) —— `paused` のような UI が読まない欄まで写し取る
 * 責務をここに持たせると、サーバーが欄を足すたびにここが取りこぼす側になる。
 */
export function parseSafety(raw: unknown): SafetyState | Malformed | undefined {
  if (raw === undefined) return undefined;
  return safetyShapeErrors(raw).length === 0 ? (raw as SafetyState) : MALFORMED;
}

/**
 * 操作モード。**モータの制御モード (position / velocity / duty) とは別物** ——
 * あちらはモータへ送る指令の種類、こちらは「制御権を誰が握っているか」。
 */
export type OperationMode = "sequence" | "manual";

/**
 * 手動操縦で連続値を送ってよい範囲とジョグ量の候補 (位置定数 yaml の `axes.<軸>.manual`)。
 * これを持たない軸は連続操作の対象外で、位置名によるプリセット指令だけができる
 * (docs/invariants.md 「手動で連続値を送ってよいのは `axes.<軸>.manual` を書いた軸だけ」)。
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
   * フィードバックから逆換算した現在値。**位置を測れない軸では null** (DC 基板は
   * エンコーダを持たないので、0 を載せると「測ったように見える 0」になる)。数値へ
   * フォールバックせず「読めていない」ことを画面に出すこと。
   */
  value: number | null;
  /** 直前に手動で送った目標値。一度も送っていなければ null */
  target: number | null;
  /** 連続操作を許した軸だけが持つ。null ならプリセット指令のみ */
  manual: ManualRange | null;
  /**
   * 左右直結ペアの現在のずれ (軸の単位)。**ずれようのない軸と測れない軸は null。**
   * サーバーが 3 層の保護と同じ `SyncGroup.deviation()` で算出した値をそのまま配る ——
   * UI 側で `motors` の位置から計算し直すと、逆回転ペアの scale の符号を 1 つ落とした
   * だけで画面が別の「ずれ」を言い出す。**0.0 は正常な値であって欠落ではない。**
   */
  deviation: number | null;
  /**
   * 偏差の許容差 (軸の単位)。`config` の `sync_tolerance` が唯一の正で、UI は
   * フォールバック値を持たない。null なら色を付けず数値も判定しない。
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

/**
 * 自作基板のセンサ入力 (原点スイッチ) 1 個の状態。**接触 (`active`) は異常ではない**
 * (原点合わせは「触れさせる」操作)。異常なのは `stale` の方。`active` の `null` は
 * 「接触を報告する手段が無いドライバ」で `—` を描く (`false` = 触れていない と混ぜない)。
 */
export interface SensorState {
  active: boolean | null;
  stale: boolean;
}

function isSensorState(value: unknown): boolean {
  if (!isObject(value)) return false;
  if (typeof value.stale !== "boolean") return false;
  return value.active === null || typeof value.active === "boolean";
}

/**
 * センサ一覧を受信境界で確定させる。未配信 (センサを配らない版のサーバー) は undefined。
 * **`?? {}` で埋めてはならない** —— 「センサが 1 本も繋がっていない構成」と見分けが
 * 付かなくなる。**センサ名は検査しない** (モータ名と同じ理由)。
 */
export function parseSensors(raw: unknown): Record<string, SensorState> | Malformed | undefined {
  if (raw === undefined) return undefined;
  if (!isObject(raw)) return MALFORMED;
  if (!Object.values(raw).every(isSensorState)) return MALFORMED;
  return raw as Record<string, SensorState>;
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
   * シーケンス実行中フラグ。**step_index / total_steps から推測しないこと** ——
   * `step_index === 0 && total_steps > 0` は準備フェーズでは常に成立し、動作確認
   * ボタンが常時無効になる。
   */
  running?: boolean;
  motors: Record<string, MotorState>;
  /**
   * 自作基板のセンサ入力 (原点スイッチ)。**`motors` とは別に持つ** —— サーバーも
   * `sensors:` を別セクションに分けており (モータ一覧に「常に 0 のモータ」を並べない
   * ため)、UI 側で混ぜるとその判断が消える。
   */
  sensors?: Record<string, SensorState> | Malformed;
  e_stop_active?: boolean;
  /** ヘルス。読めなかった配信は `MALFORMED` (**空の HealthSnapshot へ倒さない**) */
  health?: HealthSnapshot | Malformed;
  /**
   * シーケンスが最後に落ちた理由 (`SequenceTimeoutError` / `AxisSyncError` 等)。
   * 平常時は null。**失敗したときだけ自分から出す** (`ActionPanel`)。
   */
  last_error?: SequenceFailure | null;
  /**
   * 安全機構。読めなかった配信は `MALFORMED` (**空の SafetyState へ倒さない** ——
   * 平常時と見分けが付かなくなる)。
   */
  safety?: SafetyState | Malformed;
  steps?: SequenceStepInfo[];
  /**
   * 操作モードと手動操縦の軸一覧。**軸名も可動範囲も UI 側へ書かないこと** ——
   * 機構が変わって軸が増減しても UI の変更が要らない性質は、ここをそのまま描くことで
   * 成り立っている。
   */
  manual?: ManualState;
}

/** 受信条件を通ったメッセージ。UI 状態へ入れる形まで正規化してある */
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

/**
 * タイマーを読む。3 値が揃っていなければ null。
 *
 * `duration_ms <= 0` を通すと残り時間が常に 0 以下になり、画面には「試合開始と同時に
 * 時間切れ」が出る。値の欠落として扱い、表示側に「読めていない」ことを出させる。
 */
function parseTimer(raw: unknown): MatchTimer | null {
  if (!isObject(raw)) return null;
  if (typeof raw.running !== "boolean") return null;
  if (typeof raw.elapsed_ms !== "number" || !Number.isFinite(raw.elapsed_ms)) return null;
  if (typeof raw.duration_ms !== "number" || !Number.isFinite(raw.duration_ms)) return null;
  if (raw.duration_ms <= 0) return null;
  return { running: raw.running, elapsed_ms: raw.elapsed_ms, duration_ms: raw.duration_ms };
}

/** 既知の語彙に載っていなければ `MALFORMED`。黙って既定値へ倒さない */
function parseEnum<T extends string>(raw: unknown, allowed: readonly T[]): T | Malformed {
  return typeof raw === "string" && (allowed as readonly string[]).includes(raw)
    ? (raw as T)
    : MALFORMED;
}

const HEALTH_CHANGE_LEVELS: readonly HealthChangeLevel[] = ["info", "warning", "critical"];

/**
 * `HealthChangeLevel` は 3 値の union で `MALFORMED` を持てないため、読めなかった場合は
 * 必ず 3 値のどれかへ倒す。**`"critical"`（異常側）を選ぶ** —— 軽い側 (`"info"`) へ
 * 倒すと、本当に critical なイベントが型不正で無視されても画面もログも気付けない。
 *
 * **未配信 (`undefined`) と型違いを区別しない** —— `health_change` は 1 通で完結する
 * イベントでサーバーは毎回 6 欄すべてを送るので、`level` の欠落は型が読めないのと同じ異常。
 */
function parseHealthChangeLevel(raw: unknown): HealthChangeLevel {
  return typeof raw === "string" && (HEALTH_CHANGE_LEVELS as readonly string[]).includes(raw)
    ? (raw as HealthChangeLevel)
    : "critical";
}

/** どのロボットの話か決められないメッセージは捨てるしかない */
function robotOf(raw: Raw): string | null {
  return typeof raw.robot === "string" && raw.robot.length > 0 ? raw.robot : null;
}

function parseKnown(raw: Raw): ServerMessage | null {
  const robot = robotOf(raw);

  switch (raw.type) {
    case "state": {
      if (robot === null) return null;
      // **`motors` と `steps` は素通しのまま** (モータ名をハードコードしない性質は
      // 配信をそのまま UI 状態へ入れることで成り立っている)。形を確定させるのは、
      // UI が `.length` / `.filter` を直に呼ぶ `safety` と `health` だけ —— どちらも
      // レンダー本体から呼ばれるので、投げれば React ツリーごとアンマウントする
      const state = { ...(raw as unknown as RobotState) };
      const safety = parseSafety(raw.safety);
      if (safety !== undefined) state.safety = safety;
      const health = parseHealth(raw.health);
      if (health !== undefined) state.health = health;
      // センサも `Object.entries` を直に呼ばれるので形を確定させる
      const sensors = parseSensors(raw.sensors);
      if (sensors !== undefined) state.sensors = sensors;
      // 型だけ足して受信条件を書かないと「型は合っているのに画面に出ない」になる
      state.last_error = parseSequenceFailure(raw.last_error);
      return { type: "state", robot, state };
    }

    case "server_info":
      // 欠けたフラグは「無効」に倒す。開発用ボタンが本番で出るより出ない方が安全。
      // しきい値も number でなければ null にして「判定しない」へ倒す (代わりの既定値を
      // UI が持つと、それがそのまま二重管理になる)
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
          // **無検査キャストにしない。** どちらも Record の索引に使うので、
          // 未知の値はチップを無地・無文字にして画面から消す
          court: parseEnum(raw.court, MATCH_COURTS),
          phase: parseEnum(raw.phase, MATCH_PHASES),
          can_start_match: Boolean(raw.can_start_match),
          checklists: parseChecklists(raw.checklists),
          timer: parseTimer(raw.timer),
        },
      };

    case "e_stop_state":
      if (typeof raw.active !== "boolean") return null;
      // 試合中になぜ止まったか (操縦者が押したのか SyncMonitor が発報したのか) が
      // 分からないと復旧手順を選べない
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
      // **robot を要求しない。** 両ハンド統合の 1 本なので載っていない ——
      // 必須にすると動作確認の状態が 100% 捨てられる
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
