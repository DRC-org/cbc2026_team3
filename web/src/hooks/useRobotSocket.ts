import { useCallback, useEffect, useRef, useState } from "react";

import type { EpochMs, EpochSeconds } from "@/lib/time";
import { epochSecondsToMs } from "@/lib/time";
import { originWsUrl } from "@/lib/wsUrl";

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

export interface HealthChangeEvent {
  robot: string;
  level: HealthChangeLevel;
  target: string;
  from: string;
  to: string;
  message: string;
  receivedAtMs: EpochMs;
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

// motor 名で重複した record を最新で上書きしつつ、初出は末尾に追加して順序を保つ
function mergeRecord(records: MotorCheckRecord[], next: MotorCheckRecord): MotorCheckRecord[] {
  const idx = records.findIndex((r) => r.motor === next.motor);
  if (idx === -1) return [...records, next];
  const copy = records.slice();
  copy[idx] = next;
  return copy;
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

export interface CommandRejectedEvent {
  command: string;
  reason: string;
  receivedAtMs: EpochMs;
}

// WS 未接続時に UI を成立させるための初期値。サーバー接続直後に必ず上書きされる
const INITIAL_MATCH_STATE: MatchState = {
  court: "red",
  phase: "setup",
  can_start_match: false,
  checklists: {},
};

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

interface UseRobotSocketReturn {
  states: Record<string, RobotState>;
  connected: boolean;
  eStopActive: boolean;
  /** 直近の緊急停止の理由。操縦者コマンドによる停止など、理由が無い場合は null */
  eStopReason: string | null;
  healthEvents: HealthChangeEvent[];
  motorChecks: Record<string, MotorCheckState>;
  matchState: MatchState;
  rejection: CommandRejectedEvent | null;
  clearRejection: () => void;
  setEStopActive: (active: boolean) => void;
  send: (data: object) => void;
}

const RECONNECT_INTERVAL = 3000;
// 直近警告のフラッシュ表示用にのみ保持。長期履歴は不要なので少量で十分
const HEALTH_EVENT_BUFFER = 5;

/**
 * 指定 URL の WebSocket に接続し、切断中は RECONNECT_INTERVAL ごとに再接続する。
 *
 * 接続先は `lib/wsUrl.ts` が解決する（既定は配信元 origin の /ws）。
 * url が変わると現在の接続を畳んで新しい接続先へ張り直す。
 */
export function useRobotSocket(url: string = originWsUrl()): UseRobotSocketReturn {
  const [states, setStates] = useState<Record<string, RobotState>>({});
  const [connected, setConnected] = useState(false);
  const [eStopActive, setEStopActive] = useState(false);
  const [eStopReason, setEStopReason] = useState<string | null>(null);
  const [healthEvents, setHealthEvents] = useState<HealthChangeEvent[]>([]);
  const [motorChecks, setMotorChecks] = useState<Record<string, MotorCheckState>>({});
  const [matchState, setMatchState] = useState<MatchState>(INITIAL_MATCH_STATE);
  const [rejection, setRejection] = useState<CommandRejectedEvent | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  // 接続先を切り替えた後も、旧接続の close/message は非同期に届く。世代番号で弾かないと
  // 旧 URL への再接続タイマーが走り、古いサーバーの状態で画面が上書きされる
  const generationRef = useRef(0);

  const connect = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) return;

    const generation = generationRef.current;
    const isCurrent = () => generation === generationRef.current;

    const ws = new WebSocket(url);
    wsRef.current = ws;

    ws.addEventListener("open", () => {
      if (isCurrent()) setConnected(true);
    });

    ws.addEventListener("close", () => {
      if (!isCurrent()) return;
      setConnected(false);
      reconnectTimer.current = setTimeout(connect, RECONNECT_INTERVAL);
    });

    ws.addEventListener("error", () => ws.close());

    ws.addEventListener("message", (event: MessageEvent) => {
      if (!isCurrent()) return;
      try {
        const msg = JSON.parse(event.data);
        if (msg.type === "state" && msg.robot) {
          setStates((prev) => ({ ...prev, [msg.robot]: msg }));
          if (typeof msg.e_stop_active === "boolean") {
            setEStopActive(msg.e_stop_active);
            // 解除されたら理由も畳む。理由は e_stop_state だけが運ぶので、
            // 発動中の state 配信で消してはならない
            if (!msg.e_stop_active) setEStopReason(null);
          }
        } else if (msg.type === "match_state") {
          // サーバーが正。接続直後のスナップショットと変化通知の両方がここに来る
          setMatchState({
            court: msg.court as MatchCourt,
            phase: msg.phase as MatchPhase,
            can_start_match: Boolean(msg.can_start_match),
            checklists: msg.checklists ?? {},
          });
        } else if (msg.type === "command_rejected") {
          setRejection({
            command: typeof msg.command === "string" ? msg.command : "",
            reason: typeof msg.reason === "string" ? msg.reason : "",
            receivedAtMs: Date.now(),
          });
        } else if (msg.type === "e_stop_state" && typeof msg.active === "boolean") {
          setEStopActive(msg.active);
          // 試合中になぜ止まったか (操縦者が押したのか SyncMonitor が発報したのか) が
          // 分からないと復旧手順を選べない。サーバーは理由を載せて配信している
          setEStopReason(msg.active && typeof msg.reason === "string" ? msg.reason : null);
        } else if (msg.type === "health_change" && typeof msg.robot === "string") {
          const evt: HealthChangeEvent = {
            robot: msg.robot,
            level: (msg.level as HealthChangeLevel) ?? "info",
            target: typeof msg.target === "string" ? msg.target : "",
            from: typeof msg.from === "string" ? msg.from : "",
            to: typeof msg.to === "string" ? msg.to : "",
            message: typeof msg.message === "string" ? msg.message : "",
            receivedAtMs: Date.now(),
          };
          setHealthEvents((prev) => {
            // 新しい順で先頭、最大 HEALTH_EVENT_BUFFER 件のリングバッファ
            const next = [evt, ...prev];
            return next.length > HEALTH_EVENT_BUFFER ? next.slice(0, HEALTH_EVENT_BUFFER) : next;
          });
        } else if (msg.type === "motor_check_progress" && typeof msg.robot === "string") {
          const robot: string = msg.robot;
          const current: string | null = typeof msg.current === "string" ? msg.current : null;
          const index: number = typeof msg.index === "number" ? msg.index : 0;
          const total: number = typeof msg.total === "number" ? msg.total : 0;
          setMotorChecks((prev) => {
            const base = prev[robot] ?? emptyMotorCheckState();
            // 進捗の最初を受け取った時点で開始時刻を確定する
            const startedAtMs = base.startedAtMs ?? Date.now();
            return {
              ...prev,
              [robot]: {
                ...base,
                status: "running",
                current,
                progress: { index, total },
                error: null,
                snapshot: null,
                finishedAtMs: null,
                startedAtMs,
              },
            };
          });
        } else if (
          msg.type === "motor_check_record" &&
          typeof msg.robot === "string" &&
          msg.record &&
          typeof msg.record === "object"
        ) {
          const robot: string = msg.robot;
          const record = msg.record as MotorCheckRecord;
          setMotorChecks((prev) => {
            const base = prev[robot] ?? emptyMotorCheckState();
            return {
              ...prev,
              [robot]: {
                ...base,
                records: mergeRecord(base.records, record),
              },
            };
          });
        } else if (
          msg.type === "motor_check_done" &&
          typeof msg.robot === "string" &&
          msg.snapshot &&
          typeof msg.snapshot === "object"
        ) {
          const robot: string = msg.robot;
          const snapshot = msg.snapshot as CheckRunSnapshot;
          setMotorChecks((prev) => {
            const base = prev[robot] ?? emptyMotorCheckState();
            return {
              ...prev,
              [robot]: {
                ...base,
                status: "completed",
                snapshot,
                // snapshot.records が正となる。途中受信との差分を埋めるため上書き
                records: snapshot.records ?? base.records,
                current: null,
                error: null,
                // ワイヤはエポック秒。ここで ms へ正規化し、以後 UI は ms しか触らない
                startedAtMs:
                  snapshot.started_at === null || snapshot.started_at === undefined
                    ? base.startedAtMs
                    : epochSecondsToMs(snapshot.started_at),
                finishedAtMs:
                  snapshot.finished_at === null || snapshot.finished_at === undefined
                    ? Date.now()
                    : epochSecondsToMs(snapshot.finished_at),
              },
            };
          });
        } else if (msg.type === "motor_check_error" && typeof msg.robot === "string") {
          const robot: string = msg.robot;
          const message: string = typeof msg.message === "string" ? msg.message : "unknown error";
          setMotorChecks((prev) => {
            const base = prev[robot] ?? emptyMotorCheckState();
            return {
              ...prev,
              [robot]: {
                ...base,
                status: "error",
                error: message,
                current: null,
                finishedAtMs: Date.now(),
              },
            };
          });
        }
      } catch {
        // 不正な JSON は無視
      }
    });
  }, [url]);

  useEffect(() => {
    // 接続先切替の直後に旧接続の "Connected" 表示が残ると、届いていない指令を
    // 届いたものと誤認する。張り直しの間は必ず切断表示にする
    setConnected(false);
    connect();
    return () => {
      generationRef.current += 1;
      if (reconnectTimer.current) clearTimeout(reconnectTimer.current);
      wsRef.current?.close();
      wsRef.current = null;
    };
  }, [connect]);

  const send = useCallback((data: object) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify(data));
    }
  }, []);

  const clearRejection = useCallback(() => setRejection(null), []);

  return {
    states,
    connected,
    eStopActive,
    eStopReason,
    healthEvents,
    motorChecks,
    matchState,
    rejection,
    clearRejection,
    setEStopActive,
    send,
  };
}
