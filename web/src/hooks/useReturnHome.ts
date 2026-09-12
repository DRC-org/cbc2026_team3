import { useCallback, useMemo } from "react";

import { useRobotCommands, useRobotStatus } from "@/context/RobotContext";
import type { ReturnHomeSnapshot } from "@/lib/protocol";

interface UseReturnHomeReturn {
  state: ReturnHomeSnapshot;
  start: (robot: string) => void;
}

export function useReturnHome(): UseReturnHomeReturn {
  const { returnHome } = useRobotStatus();
  const { sendOrReport } = useRobotCommands();

  const start = useCallback(
    (robot: string) => {
      // 宛先を省いた形は送らない。ロボットを言わずに走らせると別の機体が動く
      sendOrReport({ type: "return_home", robot }, "原点復帰の開始");
    },
    [sendOrReport],
  );

  return useMemo(() => ({ state: returnHome, start }), [returnHome, start]);
}
