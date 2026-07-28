import { Button } from "@tsaito18/tuicss-react";

import { useRobot } from "@/context/RobotContext";

export function EStopOverlay() {
  const { eStopActive, onEStopRelease } = useRobot();

  if (!eStopActive) return null;

  return (
    <div className="tui-modal-container active">
      <div className="tui-modal">
        <div
          className="tui-window red-168 white-255-text center"
          role="alertdialog"
          aria-modal="true"
        >
          <fieldset className="tui-fieldset">
            <legend>EMERGENCY STOP</legend>
            <p className="estop-title">◆ 緊急停止中 ◆</p>
            <p>ALL MOTION HALTED</p>
            <p>全ロボットの動作を停止しています。周囲の安全を確認してください。</p>
            <div
              style={{
                marginTop: "1rem",
                display: "flex",
                justifyContent: "center",
              }}
            >
              <Button className="yellow-255" onClick={onEStopRelease}>
                ◄ Reset ►
              </Button>
            </div>
          </fieldset>
        </div>
      </div>
    </div>
  );
}
