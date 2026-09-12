import { Activity, CircleHelp } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { Modal } from "@/components/ui/Modal";
import { ClearanceWarning } from "@/components/ui/SafetyNotice";
import { useRobotStatus } from "@/context/RobotContext";
import { useMotorCheck } from "@/hooks/useMotorCheck";
import { motorCheckStatus } from "@/lib/motorCheckStatus";

export function MotorCheckButton() {
  const { connected } = useRobotStatus();
  const { state, start } = useMotorCheck();
  const [confirmOpen, setConfirmOpen] = useState(false);

  const { reasonLabel } = motorCheckStatus(state, connected);
  const disabled = reasonLabel !== null;

  const handleConfirmStart = () => {
    start();
    setConfirmOpen(false);
  };

  return (
    <>
      <Button
        tone="info"
        disabled={disabled}
        onClick={() => setConfirmOpen(true)}
        aria-label="動作確認を開始"
      >
        {state.running ? (
          <span className="loading loading-xs loading-spinner" />
        ) : (
          <Icon as={Activity} />
        )}
        {state.running ? "確認実行中..." : "動作確認"}
      </Button>
      {disabled && reasonLabel ? (
        <span className="flex basis-full items-center gap-1.5 text-base-content/70">
          <Icon as={CircleHelp} />
          {reasonLabel}
        </span>
      ) : null}

      <Modal
        open={confirmOpen}
        onClose={() => setConfirmOpen(false)}
        tone="danger"
        title="アクチュエータ動作確認"
        footer={
          <>
            <Button onClick={() => setConfirmOpen(false)}>キャンセル</Button>
            <Button tone="info" onClick={handleConfirmStart}>
              開始
            </Button>
          </>
        }
      >
        <p>
          先にリミットスイッチで零点を確定し、続けて
          <span className="font-medium text-info">メインハンドとサブハンドの全アクチュエータ</span>
          を決まった順序で 1 つずつ動かします。
        </p>
        <ClearanceWarning scope="all" />
        <p className="mt-1 text-base-content/70">
          実行中も緊急停止 (EMG STOP) は即時優先で動作します。
        </p>
      </Modal>
    </>
  );
}
