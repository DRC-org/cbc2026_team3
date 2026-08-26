import { useCallback, useMemo } from "react";

import { useRobot } from "@/context/RobotContext";
import type { MotorCheckState } from "@/hooks/useRobotSocket";
import { emptyMotorCheckState } from "@/hooks/useRobotSocket";

interface UseMotorCheckReturn {
  state: MotorCheckState;
  start: () => void;
  abort: () => void;
}

// 受信前の状態はここで作らない。reducer 側と 2 箇所に持つと片方だけ古くなる
const EMPTY_STATE: MotorCheckState = emptyMotorCheckState();

// useRobotSocket が集約した motor_check_* の state を取り出し、
// start/abort のコマンド送信を束ねるだけのプレゼンテーション層 hook
export function useMotorCheck(robot: string): UseMotorCheckReturn {
  const { motorChecks, send } = useRobot();
  const state = motorChecks[robot] ?? EMPTY_STATE;

  const start = useCallback(() => {
    send({ type: "motor_check_start", robot });
  }, [send, robot]);

  const abort = useCallback(() => {
    send({ type: "motor_check_abort", robot });
  }, [send, robot]);

  return useMemo(() => ({ state, start, abort }), [state, start, abort]);
}
