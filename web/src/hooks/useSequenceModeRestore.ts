import { useCallback, useMemo } from "react";

import { useRobotCommands, useRobotStates } from "@/context/RobotContext";

interface UseSequenceModeRestoreReturn {
  /** どれか 1 機でも手動操縦モードか。サーバーの拒否ゲートは全機を見る */
  anyManual: boolean;
  /** 知っているロボット全部を半自動へ戻す。1 通でも送れなければ false */
  restore: () => boolean;
}

export function useSequenceModeRestore(): UseSequenceModeRestoreReturn {
  const states = useRobotStates();
  const { sendOrReport } = useRobotCommands();

  const anyManual = Object.values(states).some((state) => state.manual?.mode === "manual");

  const restore = useCallback(() => {
    // 自機だけ戻しても他機が手動なら拒否されるので、全機へ送る (既に半自動なら素通り)
    let sent = true;
    for (const robot of Object.keys(states)) {
      sent =
        sendOrReport({ type: "set_operation_mode", robot, mode: "sequence" }, "半自動への復帰") &&
        sent;
    }
    return sent;
  }, [states, sendOrReport]);

  return useMemo(() => ({ anyManual, restore }), [anyManual, restore]);
}
