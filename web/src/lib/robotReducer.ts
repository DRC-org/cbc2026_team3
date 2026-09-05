import type {
  HealthChange,
  MatchState,
  MotorCheckSnapshot,
  RobotState,
  ServerInfo,
  ServerMessage,
  TuningCapture,
} from "@/lib/protocol";
import type { EpochMs } from "@/lib/time";

/**
 * WS 受信から UI 状態への遷移。**純関数**なので接続を張らずに検証できる。
 *
 * 触っていない領域は参照ごと据え置くこと。20Hz × 2 台の `state` 配信で
 * `matchState` や `motorCheck` の参照まで作り直すと、それらを読むだけの画面
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

export interface RobotUiState {
  states: Record<string, RobotState>;
  eStopActive: boolean;
  /** 直近の緊急停止の理由。操縦者コマンドによる停止など、理由が無い場合は null */
  eStopReason: string | null;
  healthEvents: HealthChangeEvent[];
  /**
   * 統合動作確認の状態。**両ハンドで 1 つ**なので Record ではない。
   *
   * サーバーが組み立てた 1 通をそのまま持つ。UI 側で進捗を継ぎ足して状態を
   * 作らないのは、途中の 1 通を落としたときに画面と機体が食い違ったまま
   * 復旧しなくなるため (再送も無いのでリロードするまで直らない)。
   */
  motorCheck: MotorCheckSnapshot;
  matchState: MatchState;
  serverInfo: ServerInfo;
  rejection: CommandRejectedEvent | null;
  /**
   * モータごとのステップ応答。キーは `robot/motor`、**新しい順で最大 2 件**。
   *
   * 2 件持つのは、調整が「変える前より良くなったか」を判断する作業だからで、
   * 最新の 1 件だけだと操縦者は前回の数字を記憶に頼って比べることになる。
   * 3 件以上は画面に出す場所が無く、持っても読まれないまま容量だけ増える。
   */
  tuningCaptures: Record<string, TuningCapture[]>;
}

/** `tuningCaptures` のキー。モータ名はロボット横断に一意だが、画面はロボットで分ける */
export function tuningKey(robot: string, motor: string): string {
  return `${robot}/${motor}`;
}

export type RobotAction =
  | { type: "message"; message: ServerMessage; nowMs: EpochMs }
  /** 操縦者操作の楽観的更新。サーバーの e_stop_state が届くまでの空白を埋める */
  | { type: "e_stop_local"; active: boolean }
  /** 切断中で送信できなかった操作。押したのに何も起きない状態を黙らせない */
  | { type: "command_unsent"; command: string; reason: string; nowMs: EpochMs }
  | { type: "clear_rejection" };

/**
 * 受信前の初期値。UI 側 (`useMotorCheck` / テストヘルパ) も必ずこれを使う。
 *
 * **`available: false` から始める。** 「起動できる」へ倒すと、配信が届く前の
 * 一瞬だけ押せるボタンが出る。押しても拒否されるだけだが、操縦者には
 * 「押したのに何も起きない」としか見えない。
 */
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
    // 受信前は「除外なし」。available:false と blocked_reason が
    // 「まだ何も届いていない」ことを既に言っているので、ここを MALFORMED から
    // 始めると接続直後に必ず「除外を読み取れません」が出る
    excluded_steps: [],
  };
}

// WS 未接続時に UI を成立させるための初期値。サーバー接続直後に必ず上書きされる
const INITIAL_MATCH_STATE: MatchState = {
  court: "red",
  phase: "setup",
  can_start_match: false,
  checklists: {},
  timer: null,
};

// 未接続時は開発用の入口を閉じておく。server_info を受けるまで開いていると、
// 接続前の一瞬だけ本番でも開発用ボタンが出る
const INITIAL_SERVER_INFO: ServerInfo = {
  dev_tools: false,
  dry_run: false,
  // server_info を受け取るまでしきい値は分からない。既定値を置くと、それが
  // サーバーの config と食い違ったまま表示される二重管理になる
  temp_warning_c: null,
  temp_critical_c: null,
};

export const INITIAL_ROBOT_UI_STATE: RobotUiState = {
  states: {},
  eStopActive: false,
  eStopReason: null,
  healthEvents: [],
  motorCheck: emptyMotorCheckState(),
  matchState: INITIAL_MATCH_STATE,
  serverInfo: INITIAL_SERVER_INFO,
  rejection: null,
  tuningCaptures: {},
};

// 直近警告のフラッシュ表示用にのみ保持。長期履歴は不要なので少量で十分
const HEALTH_EVENT_BUFFER = 5;

// 1 モータあたりに残す応答の数。最新と直前の 1 件で「良くなったか」に答えられる
const TUNING_CAPTURE_BUFFER = 2;

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

    case "server_info":
      return { ...state, serverInfo: message.serverInfo };

    case "match_state":
      return { ...state, matchState: message.matchState };

    case "e_stop_state":
      // **同値なら参照ごと据え置く。** 緊急停止中はこの 1 通が 20Hz で再配信され続け、
      // 毎回新しい state を返すと停止中ずっと全消費者が描き直される
      // (`e_stop_local` が既に同じガードを持っている)
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
      // 新しい順で先頭、最大 HEALTH_EVENT_BUFFER 件のリングバッファ
      const next = [{ ...message.event, receivedAtMs: nowMs }, ...state.healthEvents];
      return {
        ...state,
        healthEvents: next.length > HEALTH_EVENT_BUFFER ? next.slice(0, HEALTH_EVENT_BUFFER) : next,
      };
    }

    case "motor_check_state":
      // サーバーが組み立てた状態をそのまま置く。**ここで継ぎ足さない** —
      // 進捗を UI 側で組み立てると、1 通落としたときに画面だけが古い状態で
      // 固まり、再送も無いのでリロードするまで直らない
      return { ...state, motorCheck: message.motorCheck };

    case "tuning_capture": {
      const key = tuningKey(message.capture.robot, message.capture.motor);
      // 新しい順。触るのはこのモータのぶんだけで、他のモータの配列は
      // 参照ごと据え置く (グラフの再描画を無関係な記録で起こさない)
      const next = [message.capture, ...(state.tuningCaptures[key] ?? [])];
      return {
        ...state,
        tuningCaptures: {
          ...state.tuningCaptures,
          [key]: next.length > TUNING_CAPTURE_BUFFER ? next.slice(0, TUNING_CAPTURE_BUFFER) : next,
        },
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
    case "clear_rejection":
      return state.rejection === null ? state : { ...state, rejection: null };
  }
}
