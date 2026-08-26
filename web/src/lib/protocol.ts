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

export interface MotorState {
  pos: number;
  vel: number;
  torque: number;
  temp: number;
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

export type MotorCheckResult = "pending" | "running" | "passed" | "failed" | "timeout" | "skipped";
export type MotorCheckOverall = "running" | "ok" | "partial" | "failed";

export interface MotorCheckRecord {
  motor: string;
  bus: string;
  started_at: EpochSeconds;
  finished_at: EpochSeconds | null;
  result: MotorCheckResult;
  /** 到達位置を判定しない項目 (グリッパの開閉等) では期待値を持たない */
  expected: number | null;
  observed: number | null;
  detail: string | null;
}

export interface CheckRunSnapshot {
  robot: string;
  started_at: EpochSeconds;
  finished_at: EpochSeconds | null;
  overall: MotorCheckOverall;
  records: MotorCheckRecord[];
}

export type MatchCourt = "red" | "blue";
export type MatchPhase = "setup" | "ready" | "match" | "finished";
export type ChecklistRole = "main_hand" | "sub_hand";

export interface ChecklistItem {
  id: string;
  label: string;
  checked: boolean;
}

export interface ChecklistState {
  items: ChecklistItem[];
  completed: boolean;
}

export interface MatchState {
  court: MatchCourt;
  phase: MatchPhase;
  can_start_match: boolean;
  /** 完了が試合開始のゲートになるロールと、その進捗。キーの集合はサーバーが持つ */
  checklists: Record<string, ChecklistState>;
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
 * 安全機構の状態。
 *
 * `sync_violations` が空でない軸は左右のずれを検知してラッチされており、
 * 緊急停止を解除しても動かない (機構を直して解除し直す必要がある)。
 * `loops_running` / `monitors_running` が false なら 200Hz の位置制御ループか
 * 50Hz の同期監視が死んでいる。WS は繋がったままモータ状態も届き続けるため、
 * ここを読まない限り誰も気付けない。
 */
export interface SafetyState {
  sync_violations: string[];
  loops_running: boolean;
  monitors_running: boolean;
  position_loops: PositionLoopState[];
  sync_monitors: SyncMonitorState[];
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
  safety?: SafetyState;
  steps?: SequenceStepInfo[];
}

/** 受信条件を通ったメッセージ。UI 状態へ入れる形まで正規化してある */
export type ServerMessage =
  | { type: "state"; robot: string; state: RobotState }
  | { type: "match_state"; matchState: MatchState }
  | { type: "e_stop_state"; active: boolean; reason: string | null }
  | { type: "command_rejected"; command: string; reason: string }
  | { type: "health_change"; event: HealthChange }
  | {
      type: "motor_check_progress";
      robot: string;
      current: string | null;
      index: number;
      total: number;
    }
  | { type: "motor_check_record"; robot: string; record: MotorCheckRecord }
  | { type: "motor_check_done"; robot: string; snapshot: CheckRunSnapshot }
  | { type: "motor_check_error"; robot: string; message: string };

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

/** どのロボットの話か決められないメッセージは捨てるしかない */
function robotOf(raw: Raw): string | null {
  return typeof raw.robot === "string" && raw.robot.length > 0 ? raw.robot : null;
}

function parseKnown(raw: Raw): ServerMessage | null {
  const robot = robotOf(raw);

  switch (raw.type) {
    case "state":
      // モータ名をハードコードしないため、配信内容はそのまま UI 状態へ入れる
      return robot === null ? null : { type: "state", robot, state: raw as unknown as RobotState };

    case "match_state":
      // サーバーが正。接続直後のスナップショットと変化通知の両方がここに来る
      return {
        type: "match_state",
        matchState: {
          court: raw.court as MatchCourt,
          phase: raw.phase as MatchPhase,
          can_start_match: Boolean(raw.can_start_match),
          checklists: (raw.checklists as MatchState["checklists"]) ?? {},
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

    case "motor_check_progress":
      if (robot === null) return null;
      return {
        type: "motor_check_progress",
        robot,
        current: typeof raw.current === "string" ? raw.current : null,
        index: num(raw.index),
        total: num(raw.total),
      };

    case "motor_check_record":
      if (robot === null || !isObject(raw.record)) return null;
      return {
        type: "motor_check_record",
        robot,
        record: raw.record as unknown as MotorCheckRecord,
      };

    case "motor_check_done":
      if (robot === null || !isObject(raw.snapshot)) return null;
      return {
        type: "motor_check_done",
        robot,
        snapshot: raw.snapshot as unknown as CheckRunSnapshot,
      };

    case "motor_check_error":
      if (robot === null) return null;
      return { type: "motor_check_error", robot, message: str(raw.message, "unknown error") };

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
