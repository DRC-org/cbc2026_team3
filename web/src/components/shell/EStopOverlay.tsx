import { OctagonX, RotateCcw } from "lucide-react";

import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { Modal } from "@/components/ui/Modal";
import { useRobotCommands, useRobotStatus } from "@/context/RobotContext";

export function EStopOverlay() {
  const { eStopActive, eStopReason } = useRobotStatus();
  const { onEStopRelease } = useRobotCommands();

  return (
    <Modal
      open={eStopActive}
      role="alertdialog"
      tone="estop"
      title="EMERGENCY STOP"
      boxClassName="text-center"
      footer={
        <Button tone="estopReset" className="mx-auto" onClick={onEStopRelease}>
          <Icon as={RotateCcw} />
          Reset
        </Button>
      }
    >
      <div className="flex flex-col items-center gap-2 py-2">
        <Icon as={OctagonX} className="alert-blink text-[3em]" />
        <p className="text-[1.3em] font-bold tracking-wide">ALL MOTION HALTED</p>
        <p>全ロボットの動作を停止しています。周囲の安全を確認してください。</p>
        <p className="text-[1.1em] font-medium">
          {eStopReason ?? "操縦者の停止操作 (機体側の自動検知ではありません)"}
        </p>
      </div>
    </Modal>
  );
}
