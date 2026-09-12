import { Button } from "@/components/ui/Button";
import { Panel } from "@/components/ui/Panel";
import { StatusBadge } from "@/components/ui/StatusBadge";
import type { RobotCommands } from "@/context/RobotContext";
import type { ManualState } from "@/lib/protocol";

// 箱の上で位置を詰めるための刻み。大きくすると 1 押しで箱を跨ぐ
const NUDGE_STEP = 2;

interface NudgePanelProps {
  robotKey: string;
  manual: ManualState;
  blocked: boolean;
  /** このパネル固有の理由だけ。全体に効く理由 (切断中など) は上部の帯が言う */
  blockedReason: string | null;
  sendOrReport: RobotCommands["sendOrReport"];
}

/** トリガー待ちで止まっているあいだだけ出す、位置の微調整。 */
export function NudgePanel({
  robotKey,
  manual,
  blocked,
  blockedReason,
  sendOrReport,
}: NudgePanelProps) {
  // 連続値を送れる軸だけ (サーバーが `manual` を配る)。常時操作の軸は別パネルが持つ
  const axes = manual.axes.filter((axis) => axis.manual !== null && axis.manual_always !== true);
  if (axes.length === 0) return null;

  const nudge = (axis: string, delta: number) =>
    sendOrReport({ type: "manual_jog", robot: robotKey, axis, delta }, "位置の微調整");

  return (
    <Panel
      legend="位置の微調整"
      className="shrink-0"
      actions={blockedReason ? <StatusBadge tone="error">{blockedReason}</StatusBadge> : null}
    >
      <div className="flex flex-wrap gap-3">
        {axes.map((axis) => (
          <div key={axis.name} className="flex items-center gap-1.5">
            <span className="text-[0.85em] text-base-content/70">{axis.name}</span>
            <Button
              disabled={blocked}
              aria-label={`${axis.name} を ${NUDGE_STEP}${axis.unit} 戻す`}
              onClick={() => nudge(axis.name, -NUDGE_STEP)}
            >
              -{NUDGE_STEP}
            </Button>
            <Button
              disabled={blocked}
              aria-label={`${axis.name} を ${NUDGE_STEP}${axis.unit} 進める`}
              onClick={() => nudge(axis.name, NUDGE_STEP)}
            >
              +{NUDGE_STEP}
            </Button>
            <span className="text-[0.85em] text-base-content/50">{axis.unit}</span>
          </div>
        ))}
      </div>
    </Panel>
  );
}
