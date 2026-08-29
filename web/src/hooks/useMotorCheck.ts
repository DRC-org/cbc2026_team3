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
 * **robot を取らない。** 両ハンドを 1 本のシーケンスで駆動するので、
 * 機体ごとの動作確認という概念が無い。
 *
 * 可否 (`blocked_reason`) はサーバーが決める。ここで導出し直すと、サーバーが
 * 「押せる」と言っているのに画面がボタンを殺す状態が生まれる。
 */
export function useMotorCheck(): UseMotorCheckReturn {
  const { motorCheck } = useRobotStatus();
  const { send } = useRobotCommands();

  const start = useCallback(() => {
    send({ type: "motor_check_start" });
  }, [send]);

  const abort = useCallback(() => {
    send({ type: "motor_check_abort" });
  }, [send]);

  return useMemo(() => ({ state: motorCheck, start, abort }), [motorCheck, start, abort]);
}
