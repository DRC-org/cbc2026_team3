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
      overlapBackground={false}
      // 停止中であることが画面のどこを見ても分かる必要がある唯一の状態。
      // グレー基調の例外として、ここだけは面を赤で塗る
      className="modal-estop"
      windowClassName="center"
    >
      <ModalHeader>EMERGENCY STOP</ModalHeader>
      <ModalBody>
        <p className="estop-title">◆ 緊急停止中 ◆</p>
        <p>ALL MOTION HALTED</p>
        <p>全ロボットの動作を停止しています。周囲の安全を確認してください。</p>
      </ModalBody>
      <ModalFooter divider={false} style={{ marginTop: "1rem", justifyContent: "center" }}>
        <Button className="btn-estop-reset" onClick={onEStopRelease}>
          ◄ Reset ►
        </Button>
      </ModalFooter>
    </Modal>
  );
}
