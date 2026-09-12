import { useCallback, useEffect, useReducer } from "react";

import { useWebSocket } from "@/hooks/useWebSocket";
import type { LinkState } from "@/lib/linkQuality";
import { parseServerMessage } from "@/lib/protocol";
import type {
  HomingSnapshot,
  MatchState,
  MotorCheckSnapshot,
  RobotState,
  ReturnHomeSnapshot,
  ServerInfo,
  SwitchMeasureSnapshot,
} from "@/lib/protocol";
import type { CommandRejectedEvent, HealthChangeEvent } from "@/lib/robotReducer";
import { INITIAL_ROBOT_UI_STATE, robotReducer } from "@/lib/robotReducer";
import { originWsUrl } from "@/lib/wsUrl";

interface UseRobotSocketReturn {
  states: Record<string, RobotState>;
  connected: boolean;
  eStopActive: boolean;
  eStopReason: string | null;
  healthEvents: HealthChangeEvent[];
  motorCheck: MotorCheckSnapshot;
  homing: HomingSnapshot;
  returnHome: ReturnHomeSnapshot;
  switchMeasure: SwitchMeasureSnapshot;
  matchState: MatchState;
  serverInfo: ServerInfo;
  rejection: CommandRejectedEvent | null;
  link: LinkState;
  clearRejection: () => void;
  setEStopActive: (active: boolean) => void;
  reportUnsent: (command: string, reason: string) => void;
  send: (data: object) => boolean;
}

const PING_INTERVAL_MS = 1000;

export function useRobotSocket(url: string = originWsUrl()): UseRobotSocketReturn {
  const [state, dispatch] = useReducer(robotReducer, INITIAL_ROBOT_UI_STATE);

  const handleMessage = useCallback((data: string) => {
    const message = parseServerMessage(data);
    if (message) dispatch({ type: "message", message, nowMs: Date.now() });
  }, []);

  const { connected, send } = useWebSocket(url, handleMessage);

  // 往復時間を測り続ける。細い WiFi では「繋がっているのに指令が届かない」が起きるので、
  // 接続中かどうかだけでは操縦者が判断できない
  useEffect(() => {
    if (!connected) {
      dispatch({ type: "link_reset" });
      return;
    }
    const ping = () => {
      const nowMs = Date.now();
      if (send({ type: "ping", t: nowMs })) dispatch({ type: "ping_sent", nowMs });
    };
    ping();
    const timer = setInterval(ping, PING_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [connected, send]);

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
    homing: state.homing,
    returnHome: state.returnHome,
    switchMeasure: state.switchMeasure,
    matchState: state.matchState,
    serverInfo: state.serverInfo,
    rejection: state.rejection,
    link: state.link,
    clearRejection,
    setEStopActive,
    reportUnsent,
    send,
  };
}
