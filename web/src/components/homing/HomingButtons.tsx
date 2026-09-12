import { Crosshair } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { MalformedNotice } from "@/components/ui/MalformedNotice";
import { Modal } from "@/components/ui/Modal";
import { ClearanceWarning, SemiAutoRestoreNotice } from "@/components/ui/SafetyNotice";
import { useRobotStatus } from "@/context/RobotContext";
import { useHoming } from "@/hooks/useHoming";
import { homingEntry, homingStatus, robotTargets } from "@/lib/homingStatus";
import { MALFORMED } from "@/lib/protocol";
import { robotLabel } from "@/lib/robotLabel";

interface HomingButtonsProps {
  /** 担当機のぶんだけ出す。全機を回す口は動作確認なので全機版は無い */
  robot: string;
}

export function HomingButtons({ robot: only }: HomingButtonsProps) {
  const { connected } = useRobotStatus();
  const { state, start } = useHoming();
  const [pending, setPending] = useState<[string, string[]] | null>(null);

  const entry = homingEntry(state, only);
  const { reasonLabel: blocked } = homingStatus(entry, connected);
  const disabled = blocked !== null;
  const running = entry !== MALFORMED && entry?.running === true;
  const targets = robotTargets(state, only);

  if (targets === MALFORMED) {
    return <MalformedNotice subject="零点合わせの対象" />;
  }

  if (targets.length === 0) {
    return <p className="text-base-content/70">零点確定できる軸がありません。</p>;
  }

  return (
    <>
      {targets.map(([robot, axes]) => (
        <Button
          key={robot}
          disabled={disabled}
          onClick={() => setPending([robot, axes])}
          aria-label={`${robotLabel(robot)}の零点合わせを開始`}
        >
          {running ? (
            <span className="loading loading-xs loading-spinner" />
          ) : (
            <Icon as={Crosshair} />
          )}
          零点合わせ: {robotLabel(robot)}
          <span className="font-mono text-[0.85em] text-base-content/60">{axes.join(", ")}</span>
        </Button>
      ))}

      <Modal
        open={pending !== null}
        onClose={() => setPending(null)}
        tone="danger"
        title="零点合わせ"
        footer={
          <>
            <Button onClick={() => setPending(null)}>キャンセル</Button>
            <Button
              tone="info"
              onClick={() => {
                if (pending) start(pending[0], pending[1]);
                setPending(null);
              }}
            >
              開始
            </Button>
          </>
        }
      >
        <p>
          <span className="font-medium text-info">{pending ? robotLabel(pending[0]) : ""}だけ</span>
          を動かします。リミットスイッチまで寄せて零点を確定します。
        </p>
        <p className="mt-2">
          対象の軸: <span className="font-mono">{pending ? pending[1].join(", ") : ""}</span>
        </p>
        <SemiAutoRestoreNotice />
        <ClearanceWarning />
      </Modal>

      {blocked !== null ? (
        <span className="flex basis-full items-center gap-1.5 text-base-content/70">{blocked}</span>
      ) : null}
    </>
  );
}
