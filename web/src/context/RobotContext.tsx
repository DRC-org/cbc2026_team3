import { createContext, useContext, useMemo } from "react";
import type { ReactNode } from "react";

import type {
  ChecklistRole,
  MatchCourt,
  MatchState,
  MotorCheckSnapshot,
  RobotState,
  ServerInfo,
} from "@/lib/protocol";
import type { CommandRejectedEvent, HealthChangeEvent } from "@/lib/robotReducer";
import type { WsUrlSource } from "@/lib/wsUrl";

export type RobotStates = Record<string, RobotState>;

export interface RobotStatus {
  connected: boolean;
  eStopActive: boolean;
  eStopReason: string | null;
  healthEvents: HealthChangeEvent[];
  motorCheck: MotorCheckSnapshot;
  matchState: MatchState;
  serverInfo: ServerInfo;
  rejection: CommandRejectedEvent | null;
  wsUrl: string;
  wsUrlSource: WsUrlSource;
}

export interface RobotCommands {
  clearRejection: () => void;
  setWsUrl: (input: string) => boolean;
  resetWsUrl: () => void;
  openWsSettings: () => void;
  send: (data: object) => boolean;
  sendOrReport: (data: Record<string, unknown> & { type: string }, what: string) => boolean;
  onEStop: () => void;
  onEStopRelease: () => void;
  setCourt: (court: MatchCourt) => void;
  setChecklistItem: (role: ChecklistRole, itemId: string, checked: boolean) => void;
  checkAllChecklist: (role: ChecklistRole) => void;
  matchStart: () => void;
  matchFinish: () => void;
  matchReset: () => void;
}

export interface RobotContextValue extends RobotStatus, RobotCommands {
  states: RobotStates;
}

const RobotStatesContext = createContext<RobotStates | null>(null);
const RobotStatusContext = createContext<RobotStatus | null>(null);
const RobotCommandsContext = createContext<RobotCommands | null>(null);

export function RobotProvider({
  value,
  children,
}: {
  value: RobotContextValue;
  children: ReactNode;
}) {
  const {
    states,
    connected,
    eStopActive,
    eStopReason,
    healthEvents,
    motorCheck,
    matchState,
    serverInfo,
    rejection,
    wsUrl,
    wsUrlSource,
    clearRejection,
    setWsUrl,
    resetWsUrl,
    openWsSettings,
    send,
    sendOrReport,
    onEStop,
    onEStopRelease,
    setCourt,
    setChecklistItem,
    checkAllChecklist,
    matchStart,
    matchFinish,
    matchReset,
  } = value;

  const status = useMemo<RobotStatus>(
    () => ({
      connected,
      eStopActive,
      eStopReason,
      healthEvents,
      motorCheck,
      matchState,
      serverInfo,
      rejection,
      wsUrl,
      wsUrlSource,
    }),
    [
      connected,
      eStopActive,
      eStopReason,
      healthEvents,
      motorCheck,
      matchState,
      serverInfo,
      rejection,
      wsUrl,
      wsUrlSource,
    ],
  );

  const commands = useMemo<RobotCommands>(
    () => ({
      clearRejection,
      setWsUrl,
      resetWsUrl,
      openWsSettings,
      send,
      sendOrReport,
      onEStop,
      onEStopRelease,
      setCourt,
      setChecklistItem,
      checkAllChecklist,
      matchStart,
      matchFinish,
      matchReset,
    }),
    [
      clearRejection,
      setWsUrl,
      resetWsUrl,
      openWsSettings,
      send,
      sendOrReport,
      onEStop,
      onEStopRelease,
      setCourt,
      setChecklistItem,
      checkAllChecklist,
      matchStart,
      matchFinish,
      matchReset,
    ],
  );

  return (
    <RobotStatesContext.Provider value={states}>
      <RobotStatusContext.Provider value={status}>
        <RobotCommandsContext.Provider value={commands}>{children}</RobotCommandsContext.Provider>
      </RobotStatusContext.Provider>
    </RobotStatesContext.Provider>
  );
}

function useRequired<T>(value: T | null, what: string): T {
  if (value === null) throw new Error(`${what} must be used within RobotProvider`);
  return value;
}

export function useRobotStates(): RobotStates {
  return useRequired(useContext(RobotStatesContext), "useRobotStates");
}

export function useRobotStatus(): RobotStatus {
  return useRequired(useContext(RobotStatusContext), "useRobotStatus");
}

export function useRobotCommands(): RobotCommands {
  return useRequired(useContext(RobotCommandsContext), "useRobotCommands");
}
