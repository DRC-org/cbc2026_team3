import { useCallback, useReducer } from "react";

import { useWebSocket } from "@/hooks/useWebSocket";
import { parseServerMessage } from "@/lib/protocol";
import type {
  HomingSnapshot,
  MatchState,
  MotorCheckSnapshot,
  RobotState,
  ServerInfo,
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
  matchState: MatchState;
  serverInfo: ServerInfo;
  rejection: CommandRejectedEvent | null;
  clearRejection: () => void;
  setEStopActive: (active: boolean) => void;
  reportUnsent: (command: string, reason: string) => void;
  send: (data: object) => boolean;
}

export function useRobotSocket(url: string = originWsUrl()): UseRobotSocketReturn {
  const [state, dispatch] = useReducer(robotReducer, INITIAL_ROBOT_UI_STATE);

  const handleMessage = useCallback((data: string) => {
    const message = parseServerMessage(data);
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
    homing: state.homing,
    matchState: state.matchState,
    serverInfo: state.serverInfo,
    rejection: state.rejection,
    clearRejection,
    setEStopActive,
    reportUnsent,
    send,
  };
}
