import { useCallback, useEffect, useRef, useState } from "react";

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
  last_tx_at: number | null;
  last_rx_at: number | null;
  tx_error_count: number;
  rx_error_count: number;
  bus_off: boolean;
}

export interface MotorHealth {
  name: string;
  bus: string;
  state: MotorHealthState;
  last_feedback_at: number | null;
  feedback_age_ms: number | null;
  temperature: number;
  detail: string | null;
}

export interface HealthSnapshot {
  timestamp: number;
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
  receivedAt: number;
}

export type MotorCheckResult = "pending" | "running" | "passed" | "failed" | "timeout" | "skipped";
export type MotorCheckOverall = "running" | "ok" | "partial" | "failed";

export interface MotorCheckRecord {
  motor: string;
  bus: string;
  started_at: number;
  finished_at: number | null;
  result: MotorCheckResult;
  expected: number;
  observed: number | null;
  detail: string | null;
}

export interface CheckRunSnapshot {
  robot: string;
  started_at: number;
  finished_at: number | null;
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
  startedAt: number | null;
  finishedAt: number | null;
}

function emptyMotorCheckState(): MotorCheckState {
  return {
    status: "idle",
    current: null,
    progress: null,
    records: [],
    snapshot: null,
    error: null,
    startedAt: null,
    finishedAt: null,
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

export type MatchMode = "semi_auto" | "full_auto";
export type MatchCourt = "red" | "blue";
export type MatchPhase = "setup" | "ready" | "match" | "finished";
export type ChecklistRole = "monitor" | "main_hand" | "sub_hand";

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
  mode: MatchMode;
  court: MatchCourt;
  phase: MatchPhase;
  required_roles: ChecklistRole[];
  can_start_match: boolean;
  checklists: Record<string, ChecklistState>;
}

export interface CommandRejectedEvent {
  command: string;
  reason: string;
  receivedAt: number;
}

// WS 未接続時に UI を成立させるための初期値。サーバー接続直後に必ず上書きされる
const INITIAL_MATCH_STATE: MatchState = {
  mode: "semi_auto",
  court: "red",
  phase: "setup",
  required_roles: ["main_hand", "sub_hand"],
  can_start_match: false,
  checklists: {},
};

export interface SequenceStepInfo {
  index: number;
  label: string;
  require_trigger: boolean;
  auto_stop?: boolean;
}

export interface RobotState {
  robot: string;
  sequence: string;
  current_step: string | null;
  step_index: number;
  total_steps: number;
  waiting_trigger: boolean;
  motors: Record<string, MotorState>;
  e_stop_active?: boolean;
  health?: HealthSnapshot;
  steps?: SequenceStepInfo[];
}

interface UseRobotSocketReturn {
  states: Record<string, RobotState>;
  connected: boolean;
  eStopActive: boolean;
  healthEvents: HealthChangeEvent[];
  motorChecks: Record<string, MotorCheckState>;
  matchState: MatchState;
  rejection: CommandRejectedEvent | null;
  clearRejection: () => void;
  setEStopActive: (active: boolean) => void;
  send: (data: object) => void;
}

/**
 * 配信元と同じホスト・ポートの /ws に接続する。
 *
 * localhost 固定にすると、操縦者が別 PC やタブレットから開いたときに
 * 自分自身の localhost へ繋ぎにいって接続できない。サーバーは既定で
 * 0.0.0.0 を listen しており複数端末からの利用を想定しているため、
 * 接続先はページの origin から導出する。
 * (vite dev では /ws を 8080 へプロキシする — vite.config.ts 参照)
 */
function defaultWsUrl(): string {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.host}/ws`;
}

const RECONNECT_INTERVAL = 3000;
// 直近警告のフラッシュ表示用にのみ保持。長期履歴は不要なので少量で十分
const HEALTH_EVENT_BUFFER = 5;

export function useRobotSocket(url: string = defaultWsUrl()): UseRobotSocketReturn {
  const [states, setStates] = useState<Record<string, RobotState>>({});
  const [connected, setConnected] = useState(false);
  const [eStopActive, setEStopActive] = useState(false);
  const [healthEvents, setHealthEvents] = useState<HealthChangeEvent[]>([]);
  const [motorChecks, setMotorChecks] = useState<Record<string, MotorCheckState>>({});
  const [matchState, setMatchState] = useState<MatchState>(INITIAL_MATCH_STATE);
  const [rejection, setRejection] = useState<CommandRejectedEvent | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const connect = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) return;

    const ws = new WebSocket(url);
    wsRef.current = ws;

    ws.addEventListener("open", () => setConnected(true));

    ws.addEventListener("close", () => {
      setConnected(false);
      reconnectTimer.current = setTimeout(connect, RECONNECT_INTERVAL);
    });

    ws.addEventListener("error", () => ws.close());

    ws.addEventListener("message", (event: MessageEvent) => {
      try {
        const msg = JSON.parse(event.data);
        if (msg.type === "state" && msg.robot) {
          setStates((prev) => ({ ...prev, [msg.robot]: msg }));
          if (typeof msg.e_stop_active === "boolean") {
            setEStopActive(msg.e_stop_active);
          }
        } else if (msg.type === "match_state") {
          // サーバーが正。接続直後のスナップショットと変化通知の両方がここに来る
          setMatchState({
            mode: msg.mode as MatchMode,
            court: msg.court as MatchCourt,
            phase: msg.phase as MatchPhase,
            required_roles: Array.isArray(msg.required_roles) ? msg.required_roles : [],
            can_start_match: Boolean(msg.can_start_match),
            checklists: msg.checklists ?? {},
          });
        } else if (msg.type === "command_rejected") {
          setRejection({
            command: typeof msg.command === "string" ? msg.command : "",
            reason: typeof msg.reason === "string" ? msg.reason : "",
            receivedAt: Date.now(),
          });
        } else if (msg.type === "e_stop_state" && typeof msg.active === "boolean") {
          setEStopActive(msg.active);
        } else if (msg.type === "health_change" && typeof msg.robot === "string") {
          const evt: HealthChangeEvent = {
            robot: msg.robot,
            level: (msg.level as HealthChangeLevel) ?? "info",
            target: typeof msg.target === "string" ? msg.target : "",
            from: typeof msg.from === "string" ? msg.from : "",
            to: typeof msg.to === "string" ? msg.to : "",
            message: typeof msg.message === "string" ? msg.message : "",
            receivedAt: Date.now(),
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
            // 進捗の最初を受け取った時点で startedAt を確定する
            const startedAt = base.startedAt ?? Date.now() / 1000;
            return {
              ...prev,
              [robot]: {
                ...base,
                status: "running",
                current,
                progress: { index, total },
                error: null,
                snapshot: null,
                finishedAt: null,
                startedAt,
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
                startedAt: snapshot.started_at ?? base.startedAt,
                finishedAt: snapshot.finished_at ?? Date.now() / 1000,
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
                finishedAt: Date.now() / 1000,
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
    connect();
    return () => {
      if (reconnectTimer.current) clearTimeout(reconnectTimer.current);
      wsRef.current?.close();
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
    healthEvents,
    motorChecks,
    matchState,
    rejection,
    clearRejection,
    setEStopActive,
    send,
  };
}
