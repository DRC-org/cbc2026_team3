import { useCallback, useReducer } from "react";

import { useWebSocket } from "@/hooks/useWebSocket";
import { parseServerMessage } from "@/lib/protocol";
import type {
  MatchState,
  MotorCheckSnapshot,
  RobotState,
  ServerInfo,
  TuningCapture,
} from "@/lib/protocol";
import type { CommandRejectedEvent, HealthChangeEvent } from "@/lib/robotReducer";
import { INITIAL_ROBOT_UI_STATE, robotReducer } from "@/lib/robotReducer";
import { originWsUrl } from "@/lib/wsUrl";

interface UseRobotSocketReturn {
  states: Record<string, RobotState>;
  connected: boolean;
  eStopActive: boolean;
  /** 直近の緊急停止の理由。操縦者コマンドによる停止など、理由が無い場合は null */
  eStopReason: string | null;
  healthEvents: HealthChangeEvent[];
  /** 統合動作確認の状態。両ハンドで 1 つなので Record ではない */
  motorCheck: MotorCheckSnapshot;
  matchState: MatchState;
  /** 起動オプション由来の情報 (開発用コマンドの解禁など) */
  serverInfo: ServerInfo;
  rejection: CommandRejectedEvent | null;
  /** モータごとのステップ応答 (キーは `robot/motor`、新しい順で最大 2 件) */
  tuningCaptures: Record<string, TuningCapture[]>;
  clearRejection: () => void;
  setEStopActive: (active: boolean) => void;
  /** 切断中で送れなかった操作を通知枠へ流す (押したのに無反応、を作らない) */
  reportUnsent: (command: string, reason: string) => void;
  /** 送れたら true。切断中は false */
  send: (data: object) => boolean;
}

/**
 * WS 接続 (`useWebSocket`)・受信条件 (`lib/protocol`)・状態遷移 (`lib/robotReducer`) を
 * 束ねて 1 つの UI 状態にする。
 *
 * 接続先は `lib/wsUrl.ts` が解決する（既定は配信元 origin の /ws）。
 * url が変わると現在の接続を畳んで新しい接続先へ張り直す。
 */
export function useRobotSocket(url: string = originWsUrl()): UseRobotSocketReturn {
  const [state, dispatch] = useReducer(robotReducer, INITIAL_ROBOT_UI_STATE);

  const handleMessage = useCallback((data: string) => {
    const message = parseServerMessage(data);
    // 壊れた JSON と未知の type はここで落ちる (画面へは何も伝えない)
    if (message) dispatch({ type: "message", message, nowMs: Date.now() });
  }, []);

  const { connected, send } = useWebSocket(url, handleMessage);

  const clearRejection = useCallback(() => dispatch({ type: "clear_rejection" }), []);
  const reportUnsent = useCallback(
    (command: string, reason: string) =>
      dispatch({ type: "command_unsent", command, reason, nowMs: Date.now() }),
    [],
  );
  const setEStopActive = useCallback(
    (active: boolean) => dispatch({ type: "e_stop_local", active }),
    [],
  );

  return {
    states: state.states,
    connected,
    eStopActive: state.eStopActive,
    eStopReason: state.eStopReason,
    healthEvents: state.healthEvents,
    motorCheck: state.motorCheck,
    matchState: state.matchState,
    serverInfo: state.serverInfo,
    rejection: state.rejection,
    tuningCaptures: state.tuningCaptures,
    clearRejection,
    setEStopActive,
    reportUnsent,
    send,
  };
}
