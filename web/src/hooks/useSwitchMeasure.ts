import { useCallback, useMemo } from "react";

import { useRobotCommands, useRobotStatus } from "@/context/RobotContext";
import type { SwitchDirection, SwitchMeasureSnapshot } from "@/lib/protocol";

export interface SwitchMeasureOptions {
  step?: number;
  coarse_step?: number;
  limit?: number;
}

const OPTION_KEYS = ["step", "coarse_step", "limit"] as const;

interface UseSwitchMeasureReturn {
  state: SwitchMeasureSnapshot;
  start: (
    robot: string,
    axis: string,
    direction: SwitchDirection,
    options?: SwitchMeasureOptions,
  ) => void;
  /** そのロボットの対象軸を全部、両端まで寄せてスイッチ間の距離を測る */
  startDistance: (robot: string) => void;
}

export function useSwitchMeasure(): UseSwitchMeasureReturn {
  const { switchMeasure } = useRobotStatus();
  const { sendOrReport } = useRobotCommands();

  const start = useCallback(
    (
      robot: string,
      axis: string,
      direction: SwitchDirection,
      options: SwitchMeasureOptions = {},
    ) => {
      // 宛先を省いた形は送らない。ロボットと軸を言わずに走らせると別の機体が動く
      const payload: Record<string, unknown> & { type: string } = {
        type: "switch_measure_start",
        robot,
        axis,
        direction,
      };
      // 空欄はキーごと省き、サーバー側で homing の値を既定にさせる
      for (const key of OPTION_KEYS) {
        if (options[key] !== undefined) payload[key] = options[key];
      }
      sendOrReport(payload, "作動点測定の開始");
    },
    [sendOrReport],
  );

  const startDistance = useCallback(
    (robot: string) => {
      sendOrReport({ type: "switch_distance_start", robot }, "距離測定の開始");
    },
    [sendOrReport],
  );

  return useMemo(
    () => ({ state: switchMeasure, start, startDistance }),
    [switchMeasure, start, startDistance],
  );
}
