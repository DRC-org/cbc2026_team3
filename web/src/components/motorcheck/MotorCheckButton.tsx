import { Activity, CircleHelp, TriangleAlert } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { Modal } from "@/components/ui/Modal";
import { useRobotStatus } from "@/context/RobotContext";
import { useMotorCheck } from "@/hooks/useMotorCheck";
import { motorCheckStatus } from "@/lib/motorCheckStatus";

/**
 * 統合動作確認の起動ボタン。**両ハンドで 1 つ**なので robot を取らない。
 *
 * 可否の判定はサーバー (`_motor_check_deny_reason`) が唯一の持ち主で、ここは
 * 理由を表示するだけ。フェーズや緊急停止から導出し直すと、サーバーが受け付ける
 * 操作を画面が殺す状態が生まれる (かつて `StartGate` で作った失敗と同じ形)。
 */
export function MotorCheckButton({ onPanelOpen }: { onPanelOpen?: () => void }) {
  const { connected } = useRobotStatus();
  const { state, start } = useMotorCheck();
  const [confirmOpen, setConfirmOpen] = useState(false);

  // 可否の判定はパネル側と共有する。かつてパネルは `blocked_reason` しか見ておらず、
  // 切断中でも押せて、押しても何も起きず理由も出なかった
  const { reasonLabel } = motorCheckStatus(state, connected);
  const disabled = reasonLabel !== null;

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
        aria-label="動作確認を開始"
      >
        {state.running ? (
          <span className="loading loading-xs loading-spinner" />
        ) : (
          <Icon as={Activity} />
        )}
        {state.running ? "確認実行中..." : "動作確認"}
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
          <span className="font-medium text-info">メインハンドとサブハンドの全アクチュエータ</span>
          を、決まった順序で 1 つずつ動かします。
        </p>
        <p className="mt-2 flex items-center gap-1.5 text-error">
          <Icon as={TriangleAlert} />
          両機の可動範囲に人・物がないことを確認してから開始してください。
        </p>
        <p className="mt-1 text-base-content/70">
          実行中も緊急停止 (EMG STOP) は即時優先で動作します。
        </p>
      </Modal>
    </>
  );
}
