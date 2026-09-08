import { useCallback, useMemo } from "react";

import { useRobotCommands, useRobotStatus } from "@/context/RobotContext";
import type { MotorCheckSnapshot } from "@/lib/protocol";

interface UseMotorCheckReturn {
  state: MotorCheckSnapshot;
  start: () => void;
  abort: () => void;
}

/**
 * 統合動作確認の状態と操作を束ねるだけの hook。
 *
 * **robot を取らない**（両ハンドを 1 本のシーケンスで駆動する）。可否 (`blocked_reason`)
 * はサーバーが決め、ここで導出し直さない。
 *
 * **送信口は `sendOrReport` 固定。** 特に中断は無防備で、「中断」ボタンは実行中であれば
 * 常に押せる (`disabled` を持たない)。素の `send` だと切断中に押した 1 回が false を
 * 返して消え、**全アクチュエータが順に駆動されている最中に止める操作だけが痕跡なく
 * 失われる**。
 */
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
