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

/**
 * ロボット状態の配布。**購読を頻度で 3 つに割っている。**
 *
 * サーバーの配信間隔は 50ms で、ロボット 2 台ぶんなので `states` は毎秒 40 回変わる。
 * 1 つの context にまとめると、モータ温度が 0.1℃ 動いただけで指差喚呼リストも
 * タブもトーストも再描画される (会場の 1366x768 級ノート PC では試合中の入力遅延になる)。
 *
 * - `useRobotStates()`  … 高頻度テレメトリ。読む側は毎秒 40 回の再描画を受け入れる
 * - `useRobotStatus()`  … 低頻度状態 (試合状態・接続・緊急停止・ヘルス・動作確認)
 * - `useRobotCommands()`… サーバーへ送る操作。参照は原則不変
 *
 * **新しい値を足すときは頻度で置き場所を決めること。** テレメトリ由来の値を
 * status 側に置くと、分割ごと無意味になる (全消費者が 20Hz で動き出す)。
 */

/** 高頻度テレメトリ。ロボット名をキーにした最新状態 */
export type RobotStates = Record<string, RobotState>;

export interface RobotStatus {
  connected: boolean;
  eStopActive: boolean;
  /** 直近の緊急停止の理由 (SyncMonitor の発報等)。操縦者コマンドなら null */
  eStopReason: string | null;
  healthEvents: HealthChangeEvent[];
  /** 統合動作確認の状態。両ハンドで 1 つ */
  motorCheck: MotorCheckSnapshot;
  matchState: MatchState;
  /** 起動オプション由来。接続直後に 1 度届いたきり変わらない */
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
  /** 送れたら true。切断中は false (楽観的更新の可否を呼び出し側が判断できる) */
  send: (data: object) => boolean;
  onEStop: () => void;
  onEStopRelease: () => void;
  setCourt: (court: MatchCourt) => void;
  setChecklistItem: (role: ChecklistRole, itemId: string, checked: boolean) => void;
  resetChecklist: (role: ChecklistRole) => void;
  /** 開発用。サーバーが --dev-tools 起動でなければ拒否される */
  checkAllChecklist: (role: ChecklistRole) => void;
  matchStart: () => void;
  matchFinish: () => void;
  matchReset: () => void;
}

/** Provider へ渡す全量。分割は Provider の内側で行う (呼び出し側は 1 つの値を組む) */
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
    onEStop,
    onEStopRelease,
    setCourt,
    setChecklistItem,
    resetChecklist,
    checkAllChecklist,
    matchStart,
    matchFinish,
    matchReset,
  } = value;

  // 依存はフィールド単位で並べる。value 自体を依存にすると (呼び出し側は毎描画
  // 新しいオブジェクトを組むため) 分割した意味が消える
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
      onEStop,
      onEStopRelease,
      setCourt,
      setChecklistItem,
      resetChecklist,
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
      onEStop,
      onEStopRelease,
      setCourt,
      setChecklistItem,
      resetChecklist,
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

/** 高頻度テレメトリの購読。ここを読む画面は毎秒 40 回描き直される */
export function useRobotStates(): RobotStates {
  return useRequired(useContext(RobotStatesContext), "useRobotStates");
}

export function useRobotStatus(): RobotStatus {
  return useRequired(useContext(RobotStatusContext), "useRobotStatus");
}

export function useRobotCommands(): RobotCommands {
  return useRequired(useContext(RobotCommandsContext), "useRobotCommands");
}
