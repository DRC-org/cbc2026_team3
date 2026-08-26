import type {
  CheckRunSnapshot,
  HealthChange,
  MatchState,
  MotorCheckRecord,
  RobotState,
  ServerMessage,
} from "@/lib/protocol";
import type { EpochMs } from "@/lib/time";
import { epochSecondsToMs } from "@/lib/time";

/**
 * WS 受信から UI 状態への遷移。**純関数**なので接続を張らずに検証できる。
 *
 * 触っていない領域は参照ごと据え置くこと。20Hz × 2 台の `state` 配信で
 * `matchState` や `motorChecks` の参照まで作り直すと、それらを読むだけの画面
 * (チェックリスト・タブ・トースト) が毎秒 40 回再描画される。
 */

export interface HealthChangeEvent extends HealthChange {
  receivedAtMs: EpochMs;
}

export interface CommandRejectedEvent {
  command: string;
  reason: string;
  receivedAtMs: EpochMs;
  /**
   * 誰が止めたか。`server` はサーバーが条件を見て拒否したもので、条件を満たせば通る。
   * `local` は WS が切れていて送信自体ができなかったもので、機体は指令を受け取っていない。
   * 操縦者の次の一手が変わるため、同じ通知枠でも文言を分ける。
   */
  source: "server" | "local";
}

export type MotorCheckStatus = "idle" | "running" | "completed" | "error";

export interface MotorCheckState {
  status: MotorCheckStatus;
  current: string | null;
  progress: { index: number; total: number } | null;
  // 受信した record を時系列で。同じ motor の重複は最新で上書き
  records: MotorCheckRecord[];
  snapshot: CheckRunSnapshot | null;
  error: string | null;
  // 受信境界で ms へ正規化する。ワイヤの started_at/finished_at は秒なので、
  // フィールド名で単位を分けないと `new Date()` へ秒が渡って 1970 年になる
  startedAtMs: EpochMs | null;
  finishedAtMs: EpochMs | null;
}

export interface RobotUiState {
  states: Record<string, RobotState>;
  eStopActive: boolean;
  /** 直近の緊急停止の理由。操縦者コマンドによる停止など、理由が無い場合は null */
  eStopReason: string | null;
  healthEvents: HealthChangeEvent[];
  motorChecks: Record<string, MotorCheckState>;
  matchState: MatchState;
  rejection: CommandRejectedEvent | null;
}

export type RobotAction =
  | { type: "message"; message: ServerMessage; nowMs: EpochMs }
  /** 操縦者操作の楽観的更新。サーバーの e_stop_state が届くまでの空白を埋める */
  | { type: "e_stop_local"; active: boolean }
  /** 切断中で送信できなかった操作。押したのに何も起きない状態を黙らせない */
  | { type: "command_unsent"; command: string; reason: string; nowMs: EpochMs }
  | { type: "clear_rejection" };

/** 未実行の初期値。UI 側 (`useMotorCheck` / テストヘルパ) も必ずこれを使う */
export function emptyMotorCheckState(): MotorCheckState {
  return {
    status: "idle",
    current: null,
    progress: null,
    records: [],
    snapshot: null,
    error: null,
    startedAtMs: null,
    finishedAtMs: null,
  };
}

// WS 未接続時に UI を成立させるための初期値。サーバー接続直後に必ず上書きされる
const INITIAL_MATCH_STATE: MatchState = {
  court: "red",
  phase: "setup",
  can_start_match: false,
  checklists: {},
};

export const INITIAL_ROBOT_UI_STATE: RobotUiState = {
  states: {},
  eStopActive: false,
  eStopReason: null,
  healthEvents: [],
  motorChecks: {},
  matchState: INITIAL_MATCH_STATE,
  rejection: null,
};

// 直近警告のフラッシュ表示用にのみ保持。長期履歴は不要なので少量で十分
const HEALTH_EVENT_BUFFER = 5;

// motor 名で重複した record を最新で上書きしつつ、初出は末尾に追加して順序を保つ
function mergeRecord(records: MotorCheckRecord[], next: MotorCheckRecord): MotorCheckRecord[] {
  const idx = records.findIndex((r) => r.motor === next.motor);
  if (idx === -1) return [...records, next];
  const copy = records.slice();
  copy[idx] = next;
  return copy;
}

/** 1 ロボットぶんの動作確認状態だけを差し替える。他ロボットの参照は据え置く */
function patchCheck(
  state: RobotUiState,
  robot: string,
  patch: (base: MotorCheckState) => MotorCheckState,
): RobotUiState {
  const base = state.motorChecks[robot] ?? emptyMotorCheckState();
  return { ...state, motorChecks: { ...state.motorChecks, [robot]: patch(base) } };
}

function applyMessage(state: RobotUiState, message: ServerMessage, nowMs: EpochMs): RobotUiState {
  switch (message.type) {
    case "state": {
      const next: RobotUiState = {
        ...state,
        states: { ...state.states, [message.robot]: message.state },
      };
      if (typeof message.state.e_stop_active !== "boolean") return next;
      next.eStopActive = message.state.e_stop_active;
      // 解除されたら理由も畳む。理由は e_stop_state だけが運ぶので、
      // 発動中の state 配信で消してはならない
      if (!message.state.e_stop_active) next.eStopReason = null;
      return next;
    }

    case "match_state":
      return { ...state, matchState: message.matchState };

    case "e_stop_state":
      return { ...state, eStopActive: message.active, eStopReason: message.reason };

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
      // 新しい順で先頭、最大 HEALTH_EVENT_BUFFER 件のリングバッファ
      const next = [{ ...message.event, receivedAtMs: nowMs }, ...state.healthEvents];
      return {
        ...state,
        healthEvents: next.length > HEALTH_EVENT_BUFFER ? next.slice(0, HEALTH_EVENT_BUFFER) : next,
      };
    }

    case "motor_check_progress":
      return patchCheck(state, message.robot, (base) => ({
        ...base,
        status: "running",
        current: message.current,
        progress: { index: message.index, total: message.total },
        error: null,
        snapshot: null,
        finishedAtMs: null,
        // 進捗の最初を受け取った時点で開始時刻を確定する
        startedAtMs: base.startedAtMs ?? nowMs,
      }));

    case "motor_check_record":
      return patchCheck(state, message.robot, (base) => ({
        ...base,
        records: mergeRecord(base.records, message.record),
      }));

    case "motor_check_done":
      return patchCheck(state, message.robot, (base) => ({
        ...base,
        status: "completed",
        snapshot: message.snapshot,
        // snapshot.records が正となる。途中受信との差分を埋めるため上書き
        records: message.snapshot.records ?? base.records,
        current: null,
        error: null,
        // ワイヤはエポック秒。ここで ms へ正規化し、以後 UI は ms しか触らない
        startedAtMs:
          message.snapshot.started_at === null || message.snapshot.started_at === undefined
            ? base.startedAtMs
            : epochSecondsToMs(message.snapshot.started_at),
        finishedAtMs:
          message.snapshot.finished_at === null || message.snapshot.finished_at === undefined
            ? nowMs
            : epochSecondsToMs(message.snapshot.finished_at),
      }));

    case "motor_check_error":
      return patchCheck(state, message.robot, (base) => ({
        ...base,
        status: "error",
        error: message.message,
        current: null,
        finishedAtMs: nowMs,
      }));
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
