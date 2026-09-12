import { Ruler } from "lucide-react";
import { useState } from "react";

import { SwitchMeasureBadge } from "@/components/homing/SwitchMeasureResult";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { MalformedNotice } from "@/components/ui/MalformedNotice";
import { Modal } from "@/components/ui/Modal";
import { ClearanceWarning, SemiAutoRestoreNotice } from "@/components/ui/SafetyNotice";
import { useRobotStatus } from "@/context/RobotContext";
import { useSwitchMeasure } from "@/hooks/useSwitchMeasure";
import { MALFORMED } from "@/lib/protocol";
import { robotLabel } from "@/lib/robotLabel";
import { switchDistanceStatus } from "@/lib/switchMeasureStatus";

interface SwitchDistanceButtonProps {
  /** 担当機のぶんだけ出す */
  robot: string;
}

/**
 * 操縦者用。1 つのボタンでそのロボットの対象軸を全部、両端まで寄せてスイッチ間の距離を測る。
 * 刻みは homing の既定に任せる。スイッチ 1 本ずつ刻みを変えて測るのは Monitor の `SwitchMeasurePanel`
 */
export function SwitchDistanceButton({ robot }: SwitchDistanceButtonProps) {
  const { connected } = useRobotStatus();
  const { state, startDistance } = useSwitchMeasure();
  const [pending, setPending] = useState(false);

  const { outcome, reasonLabel: blocked } = switchDistanceStatus(state, connected);

  if (state.targets === MALFORMED) {
    return <MalformedNotice subject="距離測定の対象" />;
  }

  const axes = state.targets[robot] ?? [];
  if (axes.length === 0) return null;

  const mine = state.robot === robot && state.distances !== null;

  return (
    <>
      <Button
        disabled={blocked !== null}
        onClick={() => setPending(true)}
        aria-label={`${robotLabel(robot)}のリミットスイッチ間の距離測定を開始`}
      >
        {mine && outcome === "running" ? (
          <span className="loading loading-xs loading-spinner" />
        ) : (
          <Icon as={Ruler} />
        )}
        リミットスイッチ間の距離測定
        <span className="font-mono text-[0.85em] text-base-content/60">{axes.join(", ")}</span>
      </Button>

      {mine ? (
        <div className="flex basis-full flex-col gap-1">
          <div className="flex items-center gap-2">
            <span className="text-base-content/70">距離測定</span>
            <SwitchMeasureBadge outcome={outcome} />
            {state.running && state.axis ? (
              <span className="min-w-0 truncate font-mono text-info">{state.axis}</span>
            ) : null}
          </div>
          {state.error ? <p className="text-error">{state.error}</p> : null}
          {state.distances === MALFORMED ? (
            <MalformedNotice subject="距離測定の結果" />
          ) : (
            <ul className="flex flex-col">
              {state.distances?.map((item) => (
                <li key={item.axis} className="flex items-baseline gap-2 px-1 py-[0.15rem]">
                  <span className="font-mono">{item.axis}</span>
                  {item.distance !== null ? (
                    <span className="font-mono tabular-nums">
                      {item.distance} {item.unit}
                    </span>
                  ) : null}
                  {item.step !== null ? (
                    <span className="text-base-content/70">
                      (刻み {item.step} {item.unit}
                      {item.coarse_step !== null
                        ? ` / 粗刻み ${item.coarse_step} ${item.unit}`
                        : ""}
                      )
                    </span>
                  ) : null}
                  {item.error ? <span className="min-w-0 text-error">{item.error}</span> : null}
                </li>
              ))}
            </ul>
          )}
        </div>
      ) : null}

      <Modal
        open={pending}
        onClose={() => setPending(false)}
        tone="danger"
        title="リミットスイッチ間の距離測定"
        footer={
          <>
            <Button onClick={() => setPending(false)}>キャンセル</Button>
            <Button
              tone="info"
              onClick={() => {
                startDistance(robot);
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
          を動かします。軸ごとに両端のリミットスイッチまで寄せ、スイッチが入る点どうしの距離を測ります
          (刻みは homing の既定)。
        </p>
        <p className="mt-2">
          対象の軸: <span className="font-mono">{axes.join(", ")}</span>
        </p>
        <SemiAutoRestoreNotice />
        <ClearanceWarning />
      </Modal>
    </>
  );
}
