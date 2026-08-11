import { Button, Modal, ModalBody, ModalFooter, ModalHeader } from "@tsaito18/tuicss-react";

import { useRobot } from "@/context/RobotContext";

export function EStopOverlay() {
  const { eStopActive, onEStopRelease } = useRobot();

  return (
    // 解除経路を Reset ボタンのみに限定する（Esc / 背景クリックでの誤解除を禁止）
    <Modal
      open={eStopActive}
      dismissable={false}
      role="alertdialog"
      windowClassName="red-168 white-255-text center"
    >
      <ModalHeader>EMERGENCY STOP</ModalHeader>
      <ModalBody>
        <p className="estop-title">◆ 緊急停止中 ◆</p>
        <p>ALL MOTION HALTED</p>
        <p>全ロボットの動作を停止しています。周囲の安全を確認してください。</p>
      </ModalBody>
      <ModalFooter divider={false} style={{ marginTop: "1rem", justifyContent: "center" }}>
        <Button className="yellow-255" onClick={onEStopRelease}>
          ◄ Reset ►
        </Button>
      </ModalFooter>
    </Modal>
  );
}
