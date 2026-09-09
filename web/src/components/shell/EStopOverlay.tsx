import { EyeOff, OctagonX, RotateCcw } from "lucide-react";

import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { Modal } from "@/components/ui/Modal";
import { useRobotCommands, useRobotStatus } from "@/context/RobotContext";

export function EStopOverlay() {
  const { eStopActive, eStopReason, eStopOverlayHidden, serverInfo } = useRobotStatus();
  const { onEStopRelease, hideEStopOverlay } = useRobotCommands();

  return (
    <Modal
      open={eStopActive && !eStopOverlayHidden}
      role="alertdialog"
      tone="estop"
      title="EMERGENCY STOP"
      boxClassName="text-center"
      footer={
        // Reset を中央に据えたまま、非表示は右端へ最大限離す (隣り合わせると解除の誤爆になる)。
        <div className="flex w-full items-center gap-2">
          <div className="flex-1" />
          <Button tone="estopReset" onClick={onEStopRelease}>
            <Icon as={RotateCcw} />
            Reset
          </Button>
          <div className="flex flex-1 justify-end">
            {serverInfo.dev_tools ? (
              <Button
                tone="warn"
                onClick={hideEStopOverlay}
                aria-label="緊急停止ダイアログを開発用に非表示にする"
              >
                <Icon as={EyeOff} />
                DEV 非表示
              </Button>
            ) : null}
          </div>
        </div>
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
