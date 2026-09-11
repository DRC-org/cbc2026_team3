import { Crosshair, TriangleAlert } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { Modal } from "@/components/ui/Modal";
import { useRobotStatus } from "@/context/RobotContext";
import { useHoming } from "@/hooks/useHoming";
import { useSequenceModeRestore } from "@/hooks/useSequenceModeRestore";
import { homingEntry, homingStatus, robotTargets } from "@/lib/homingStatus";
import { MALFORMED } from "@/lib/protocol";
import { robotLabel } from "@/lib/robotLabel";

interface HomingButtonsProps {
  /** 担当機のぶんだけ出す。全機を回す口は動作確認なので全機版は無い */
  robot: string;
}

export function HomingButtons({ robot: only }: HomingButtonsProps) {
  const { connected, eStopActive } = useRobotStatus();
  const { state, start } = useHoming();
  const { anyManual, restore } = useSequenceModeRestore();
  const [pending, setPending] = useState<[string, string[]] | null>(null);

  const entry = homingEntry(state, only);
  // 手動中の拒否は押せば自分で解消する (全機を半自動へ戻してから送る) ので、その理由では塞がない
  const { reasonLabel } = homingStatus(entry, connected);
  const blocked = anyManual && connected && !eStopActive ? null : reasonLabel;
  const disabled = blocked !== null;
  const running = entry !== MALFORMED && entry?.running === true;
  const targets = robotTargets(state, only);

  if (targets === MALFORMED) {
    return (
      <p className="flex items-center gap-1.5 text-warning">
        <Icon as={TriangleAlert} />
        零点合わせの対象を読み取れませんでした (配信の形が読めていません)
      </p>
    );
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
                if (pending && restore()) start(pending[0], pending[1]);
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
        {anyManual ? (
          <p className="mt-2">手動操縦を抜け、全機を半自動へ戻してから開始します。</p>
        ) : null}
        <p className="mt-2 flex items-center gap-1.5 text-error">
          <Icon as={TriangleAlert} />
          この機体の可動範囲に人・物がないことを確認してから開始してください。
        </p>
      </Modal>

      {blocked !== null ? (
        <span className="flex basis-full items-center gap-1.5 text-base-content/70">{blocked}</span>
      ) : null}
    </>
  );
}
