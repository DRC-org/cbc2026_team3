import { OctagonX, RotateCcw } from "lucide-react";

import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { Modal } from "@/components/ui/Modal";
import { useRobot } from "@/context/RobotContext";

export function EStopOverlay() {
  const { eStopActive, eStopReason, onEStopRelease } = useRobot();

  return (
    // onClose を渡さないことで解除経路を Reset ボタンのみに限定する
    // （Esc / 背景クリックでの誤解除を構造的に禁止する）
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
        {/* 停止理由。SyncMonitor の左右ペア軸ずれ検出もこの経路で理由付きに発動する。
            「誰かが押したのか、機体が壊れたのか」が分からないと復旧手順を選べない */}
        <p className="text-[1.1em] font-medium">
          {eStopReason ?? "操縦者の停止操作 (機体側の自動検知ではありません)"}
        </p>
      </div>
    </Modal>
  );
}
