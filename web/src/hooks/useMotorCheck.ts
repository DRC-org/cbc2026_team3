import { useCallback, useMemo } from "react";

import { useRobotCommands, useRobotStatus } from "@/context/RobotContext";
import type { MotorCheckSnapshot } from "@/lib/protocol";

interface UseMotorCheckReturn {
  state: MotorCheckSnapshot;
  start: () => void;
  abort: () => void;
}

export function useMotorCheck(): UseMotorCheckReturn {
  const { motorCheck } = useRobotStatus();
  const { sendOrReport } = useRobotCommands();

  const start = useCallback(() => {
    sendOrReport({ type: "motor_check_start" }, "動作確認の開始");
  }, [sendOrReport]);

  const abort = useCallback(() => {
    sendOrReport({ type: "motor_check_abort" }, "動作確認の中断");
  }, [sendOrReport]);

  return useMemo(() => ({ state: motorCheck, start, abort }), [motorCheck, start, abort]);
}
