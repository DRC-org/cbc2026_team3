import { OctagonX, RotateCcw } from "lucide-react";

import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { useRobotCommands, useRobotStatus } from "@/context/RobotContext";

// オーバーレイを畳んでいる間の代替表示。解除手段 (Reset) を画面から消さないために必ず帯へ載せる。
export function EStopBanner() {
  const { eStopActive, eStopReason, eStopOverlayHidden } = useRobotStatus();
  const { onEStopRelease } = useRobotCommands();

  if (!eStopActive || !eStopOverlayHidden) return null;

  return (
    <div
      role="alert"
      className="alert flex w-full shrink-0 items-center justify-center gap-3 border-x-0 border-t-0 px-3 py-1 alert-error"
    >
      <Icon as={OctagonX} className="alert-blink text-[1.2em]" />
      <span className="font-bold">EMERGENCY STOP — 全ロボット停止中</span>
      <span>{eStopReason ?? "操縦者の停止操作 (機体側の自動検知ではありません)"}</span>
      <Button tone="estopReset" onClick={onEStopRelease}>
        <Icon as={RotateCcw} />
        Reset
      </Button>
    </div>
  );
}
