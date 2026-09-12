import { House } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { MalformedNotice } from "@/components/ui/MalformedNotice";
import { Modal } from "@/components/ui/Modal";
import { ClearanceWarning, SemiAutoRestoreNotice } from "@/components/ui/SafetyNotice";
import { useRobotStatus } from "@/context/RobotContext";
import { useReturnHome } from "@/hooks/useReturnHome";
import { useSequenceModeRestore } from "@/hooks/useSequenceModeRestore";
import { MALFORMED } from "@/lib/protocol";
import { robotLabel } from "@/lib/robotLabel";

interface ReturnHomeButtonProps {
  /** 担当機のぶんだけ出す。宛先を省いた全機一括は存在しない */
  robot: string;
}

const DISCONNECTED = "切断中のため不可";

export function ReturnHomeButton({ robot }: ReturnHomeButtonProps) {
  const { connected, eStopActive } = useRobotStatus();
  const { state, start } = useReturnHome();
  const { anyManual, restore } = useSequenceModeRestore();
  const [pending, setPending] = useState(false);

  if (state.robots === MALFORMED) {
    return <MalformedNotice subject="原点復帰の状態" />;
  }

  const entry = Object.hasOwn(state.robots, robot) ? state.robots[robot] : undefined;
  if (!state.available || entry === undefined) {
    return <p className="text-base-content/70">原点復帰できる手順がありません。</p>;
  }

  const reasonLabel = connected ? entry.blocked_reason : DISCONNECTED;
  // 手動中の拒否は押せば自分で解消する (全機を半自動へ戻してから送る) ので、その理由では塞がない
  const blocked = anyManual && connected && !eStopActive ? null : reasonLabel;
  const progress =
    entry.running && entry.current_step !== null && entry.steps !== MALFORMED
      ? `${entry.current_step}/${entry.steps}`
      : null;

  return (
    <>
      <Button
        disabled={blocked !== null}
        onClick={() => setPending(true)}
        aria-label={`${robotLabel(robot)}の原点復帰を開始`}
      >
        {entry.running ? (
          <span className="loading loading-xs loading-spinner" />
        ) : (
          <Icon as={House} />
        )}
        原点復帰: {robotLabel(robot)}
        {progress !== null ? (
          <span className="font-mono text-[0.85em] text-base-content/60">{progress}</span>
        ) : null}
      </Button>

      <Modal
        open={pending}
        onClose={() => setPending(false)}
        tone="danger"
        title="原点復帰"
        footer={
          <>
            <Button onClick={() => setPending(false)}>キャンセル</Button>
            <Button
              tone="info"
              onClick={() => {
                if (restore()) start(robot);
                setPending(false);
              }}
            >
              開始
            </Button>
          </>
        }
      >
        <p>
          <span className="font-medium text-info">{robotLabel(robot)}だけ</span>
          を動かします。試合シーケンスの初期位置へ戻します。
        </p>
        {anyManual ? <SemiAutoRestoreNotice /> : null}
        <ClearanceWarning />
      </Modal>

      {blocked !== null ? (
        <span className="flex basis-full items-center gap-1.5 text-base-content/70">{blocked}</span>
      ) : null}
    </>
  );
}
