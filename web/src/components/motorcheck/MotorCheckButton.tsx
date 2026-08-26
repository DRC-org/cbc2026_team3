import { Activity, CircleHelp, TriangleAlert } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { Modal } from "@/components/ui/Modal";
import { useRobot } from "@/context/RobotContext";
import { useMotorCheck } from "@/hooks/useMotorCheck";

interface MotorCheckButtonProps {
  robotName: string;
  onPanelOpen?: () => void;
}

export function MotorCheckButton({ robotName, onPanelOpen }: MotorCheckButtonProps) {
  const { eStopActive, connected, matchState } = useRobot();
  const { state, start } = useMotorCheck(robotName);
  const [confirmOpen, setConfirmOpen] = useState(false);

  // 可否の判定はサーバー側 (`_PHASE_GATES["motor_check_start"]`) に合わせてフェーズで行う。
  // 以前はステップ番号から「シーケンス実行中か」を推定していたが、準備中は step_index=0 /
  // total_steps>0 が常に成立するため、動作確認が主役のセッティングタイムで
  // ボタンが常時無効になっていた（サーバーはこのフェーズでこそ受け付ける）。
  const inMatch = matchState.phase === "match";
  const checkRunning = state.status === "running";
  const disabled = eStopActive || inMatch || checkRunning || !connected;

  const reasonLabel = !connected
    ? "切断中のため不可"
    : eStopActive
      ? "緊急停止中は不可"
      : inMatch
        ? "試合中は動作確認を実行できません"
        : checkRunning
          ? "動作確認 実行中"
          : null;

  const handleConfirmStart = () => {
    start();
    setConfirmOpen(false);
    onPanelOpen?.();
  };

  return (
    <>
      <Button
        tone="info"
        disabled={disabled}
        onClick={() => setConfirmOpen(true)}
        aria-label={`${robotName} の動作確認を開始`}
      >
        {checkRunning ? (
          <span className="loading loading-xs loading-spinner" />
        ) : (
          <Icon as={Activity} />
        )}
        {checkRunning ? "確認実行中..." : "動作確認"}
      </Button>
      {/* Tooltip は使えないため無効化理由をテキストで併記する。
          理由文は長く、ボタンと同じ行に流すと折り返して行が 2 段に化ける。
          flex-wrap の親の中で必ず行頭から始まるよう basis-full を与える */}
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
        title="Operational Check"
        footer={
          <>
            <Button onClick={() => setConfirmOpen(false)}>Cancel</Button>
            <Button tone="info" onClick={handleConfirmStart}>
              Start
            </Button>
          </>
        }
      >
        <p>
          <span className="font-medium text-info">{robotName}</span>{" "}
          の全モータを順番に微小駆動します。
        </p>
        <p className="mt-2 flex items-center gap-1.5 text-error">
          <Icon as={TriangleAlert} />
          周囲の安全を確認してから開始してください。
        </p>
        <p className="mt-1 text-base-content/70">
          実行中も緊急停止 (EMG STOP) は即時優先で動作します。
        </p>
      </Modal>
    </>
  );
}
