import { createContext, useContext } from "react";
import type { ReactNode } from "react";

import type {
  ChecklistRole,
  CommandRejectedEvent,
  HealthChangeEvent,
  MatchCourt,
  MatchMode,
  MatchState,
  MotorCheckState,
  RobotState,
} from "@/hooks/useRobotSocket";
import type { WsUrlSource } from "@/lib/wsUrl";

interface RobotContextValue {
  states: Record<string, RobotState>;
  connected: boolean;
  eStopActive: boolean;
  healthEvents: HealthChangeEvent[];
  motorChecks: Record<string, MotorCheckState>;
  matchState: MatchState;
  rejection: CommandRejectedEvent | null;
  clearRejection: () => void;
  wsUrl: string;
  wsUrlSource: WsUrlSource;
  setWsUrl: (input: string) => boolean;
  resetWsUrl: () => void;
  openWsSettings: () => void;
  send: (data: object) => void;
  onEStop: () => void;
  onEStopRelease: () => void;
  setMode: (mode: MatchMode) => void;
  setCourt: (court: MatchCourt) => void;
  setChecklistItem: (role: ChecklistRole, itemId: string, checked: boolean) => void;
  resetChecklist: (role: ChecklistRole) => void;
  matchStart: () => void;
  matchFinish: () => void;
  matchReset: () => void;
}

const RobotContext = createContext<RobotContextValue | null>(null);

export function RobotProvider({
  value,
  children,
}: {
  value: RobotContextValue;
  children: ReactNode;
}) {
  return <RobotContext.Provider value={value}>{children}</RobotContext.Provider>;
}

export function useRobot(): RobotContextValue {
  const ctx = useContext(RobotContext);
  if (!ctx) throw new Error("useRobot must be used within RobotProvider");
  return ctx;
}
