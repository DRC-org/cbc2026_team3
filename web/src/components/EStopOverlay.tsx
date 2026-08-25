import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";
import { useRobot } from "@/context/RobotContext";

export function EStopOverlay() {
  const { eStopActive, onEStopRelease } = useRobot();

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
          ◄ Reset ►
        </Button>
      }
    >
      <p className="alert-blink my-1">◆ 緊急停止中 ◆</p>
      <p>ALL MOTION HALTED</p>
      <p>全ロボットの動作を停止しています。周囲の安全を確認してください。</p>
    </Modal>
  );
}
