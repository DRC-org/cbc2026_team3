import { useCallback, useMemo } from "react";

import { useRobotCommands, useRobotStatus } from "@/context/RobotContext";
import type { HomingSnapshot } from "@/lib/protocol";

interface UseHomingReturn {
  state: HomingSnapshot;
  start: (robot: string, axes?: string[]) => void;
}

export function useHoming(): UseHomingReturn {
  const { homing } = useRobotStatus();
  const { sendOrReport } = useRobotCommands();

  const start = useCallback(
    (robot: string, axes?: string[]) => {
      // 宛先を省いた形は送らない。ロボットを言わずに走らせると別の機体が動く
      sendOrReport({ type: "homing_start", robot, ...(axes ? { axes } : {}) }, "零点合わせの開始");
    },
    [sendOrReport],
  );

  return useMemo(() => ({ state: homing, start }), [homing, start]);
}
