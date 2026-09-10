import { Ruler, TriangleAlert } from "lucide-react";
import { useState } from "react";

import { SwitchMeasureBadge, SwitchMeasureResult } from "@/components/homing/SwitchMeasureResult";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { Modal } from "@/components/ui/Modal";
import { useRobotStatus } from "@/context/RobotContext";
import { useSequenceModeRestore } from "@/hooks/useSequenceModeRestore";
import { useSwitchMeasure } from "@/hooks/useSwitchMeasure";
import { MALFORMED } from "@/lib/protocol";
import type { SwitchDirection } from "@/lib/protocol";
import { robotLabel } from "@/lib/robotLabel";
import { SWITCH_DIRECTIONS, directionLabel, switchMeasureStatus } from "@/lib/switchMeasureStatus";

interface SwitchMeasureButtonsProps {
  /** 担当機のぶんだけ出す */
  robot: string;
}

/**
 * 操縦者用。配信された軸 × 向きごとに 1 つずつボタンを出し、刻みは homing の既定に任せる。
 * 刻みを変えたいときは Monitor 側の `SwitchMeasurePanel`
 */
export function SwitchMeasureButtons({ robot }: SwitchMeasureButtonsProps) {
  const { connected, eStopActive } = useRobotStatus();
  const { state, start } = useSwitchMeasure();
  const { anyManual, restore } = useSequenceModeRestore();
  const [pending, setPending] = useState<[string, SwitchDirection] | null>(null);

  // 手動中の拒否は押せば自分で解消する (全機を半自動へ戻してから送る) ので、その理由では塞がない
  const { outcome, reasonLabel } = switchMeasureStatus(state, connected);
  const blocked = anyManual && connected && !eStopActive ? null : reasonLabel;

  if (state.targets === MALFORMED) {
    return (
      <p className="flex items-center gap-1.5 text-warning">
        <Icon as={TriangleAlert} />
        作動点測定の対象を読み取れませんでした (配信の形が読めていません)
      </p>
    );
  }

  const axes = state.targets[robot] ?? [];
  if (axes.length === 0) {
    return <p className="text-base-content/70">作動点を測定できる軸がありません。</p>;
  }

  const isCurrent = (axis: string, direction: SwitchDirection) =>
    state.robot === robot && state.axis === axis && state.direction === direction;

  return (
    <>
      <ul className="flex flex-col gap-1">
        {axes.map((axis) => (
          <li key={axis} className="flex flex-col gap-1">
            <div className="flex flex-wrap items-center gap-2">
              <span className="min-w-0 truncate font-mono">{axis}</span>
              {SWITCH_DIRECTIONS.map((direction) => (
                <Button
                  key={direction}
                  disabled={blocked !== null}
                  onClick={() => setPending([axis, direction])}
                  aria-label={`${axis} の${directionLabel(direction)}の作動点測定を開始`}
                >
                  {state.running && isCurrent(axis, direction) ? (
                    <span className="loading loading-xs loading-spinner" />
                  ) : (
                    <Icon as={Ruler} />
                  )}
                  {directionLabel(direction)}
                </Button>
              ))}
            </div>
            {state.robot === robot && state.axis === axis && state.direction !== null ? (
              <div className="flex flex-col gap-1 pl-2">
                <div className="flex items-center gap-2">
                  <SwitchMeasureBadge outcome={outcome} />
                  <span className="text-base-content/70">{directionLabel(state.direction)}</span>
                </div>
                <SwitchMeasureResult state={state} />
              </div>
            ) : null}
          </li>
        ))}
      </ul>

      {blocked !== null ? <span className="text-base-content/70">{blocked}</span> : null}

      <Modal
        open={pending !== null}
        onClose={() => setPending(null)}
        tone="danger"
        title="作動点測定"
        footer={
          <>
            <Button onClick={() => setPending(null)}>キャンセル</Button>
            <Button
              tone="info"
              onClick={() => {
                if (pending && restore()) start(robot, pending[0], pending[1]);
                setPending(null);
              }}
            >
              開始
            </Button>
          </>
        }
      >
        <p>
          <span className="font-medium text-info">{robotLabel(robot)}だけ</span>
          を動かします。リミットスイッチが入る位置と離れる位置を測ります。
        </p>
        <p className="mt-2">
          向き: <span className="font-mono">{pending ? pending[0] : ""}</span> を{" "}
          <span className="font-medium">{pending ? directionLabel(pending[1]) : ""}</span>
          へ動かします (刻みは homing の既定)
        </p>
        {anyManual ? (
          <p className="mt-2">手動操縦を抜け、全機を半自動へ戻してから開始します。</p>
        ) : null}
        <p className="mt-2 flex items-center gap-1.5 text-error">
          <Icon as={TriangleAlert} />
          この機体の可動範囲に人・物がないことを確認してから開始してください。
        </p>
      </Modal>
    </>
  );
}
