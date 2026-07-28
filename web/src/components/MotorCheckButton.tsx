import { Button, Modal, Window, Fieldset } from "@tsaito18/tuicss-react";
import { useState } from "react";

import { useRobot } from "@/context/RobotContext";
import { useMotorCheck } from "@/hooks/useMotorCheck";

interface MotorCheckButtonProps {
  robotName: string;
  onPanelOpen?: () => void;
}

export function MotorCheckButton({
  robotName,
  onPanelOpen,
}: MotorCheckButtonProps) {
  const { eStopActive, connected, states } = useRobot();
  const { state, start } = useMotorCheck(robotName);
  const [confirmOpen, setConfirmOpen] = useState(false);

  const robotState = states[robotName];
  const sequenceRunning = Boolean(
    robotState &&
    robotState.total_steps > 0 &&
    !robotState.waiting_trigger &&
    robotState.step_index + 1 < robotState.total_steps,
  );

  const checkRunning = state.status === "running";
  const disabled = eStopActive || sequenceRunning || checkRunning || !connected;

  const reasonLabel = !connected
    ? "切断中のため不可"
    : eStopActive
      ? "緊急停止中は不可"
      : sequenceRunning
        ? "シーケンス実行中は不可"
        : checkRunning
          ? "動作確認 実行中"
          : null;

  const handleConfirmStart = () => {
    start();
    setConfirmOpen(false);
    onPanelOpen?.();
  };

  return (
    <div style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
      <Button
        className="cyan-255"
        disabled={disabled}
        onClick={() => setConfirmOpen(true)}
        aria-label={`${robotName} の動作確認を開始`}
      >
        {checkRunning ? "► 確認実行中..." : "▮ 動作確認"}
      </Button>
      {/* Tooltip は使えないため無効化理由を等幅テキストで併記する。 */}
      {disabled && reasonLabel ? (
        <span style={{ opacity: 0.7 }}>[?] {reasonLabel}</span>
      ) : null}

      <Modal open={confirmOpen} onClose={() => setConfirmOpen(false)}>
        <Window className="red-168">
          <Fieldset legend="Operational Check">
            Some content inside the fieldset.
          </Fieldset>
        </Window>
        <p>
          <span className="info-text">{robotName}</span>{" "}
          の全モータを順番に微小駆動します。
        </p>
        <p
          className="danger-text"
          style={{
            marginTop: "0.5rem",
          }}
        >
          ⚠ 周囲の安全を確認してから開始してください。
        </p>
        <p style={{ marginTop: "0.25rem", opacity: 0.8 }}>
          実行中も緊急停止 (EMG STOP) は即時優先で動作します。
        </p>
        <div
          style={{
            display: "flex",
            justifyContent: "flex-end",
            gap: "0.5rem",
          }}
        >
          <Button onClick={() => setConfirmOpen(false)}>Cancel</Button>
          <Button className="cyan-255" onClick={handleConfirmStart}>
            Start
          </Button>
        </div>
      </Modal>
    </div>
  );
}
