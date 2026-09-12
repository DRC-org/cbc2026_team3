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
  /** リンク機構の中心をずらす口。トリガー待ちに依らず手動と同じゲート */
  centerBlocked: boolean;
  sendOrReport: RobotCommands["sendOrReport"];
}

/** トリガー待ちで止まっているあいだだけ出す、位置の微調整。 */
export function NudgePanel({
  robotKey,
  manual,
  blocked,
  blockedReason,
  centerBlocked,
  sendOrReport,
}: NudgePanelProps) {
  // 連続値を送れる軸だけ (サーバーが `manual` を配る)。常時操作の軸は別パネルが持つ
  const axes = manual.axes.filter((axis) => axis.manual !== null && axis.manual_always !== true);
  // リンク機構の軸は中心を左右へずらせる (サーバーが `linkage` を配る)
  const linked = manual.axes.filter((axis) => axis.linkage !== null);
  if (axes.length === 0 && linked.length === 0) return null;

  const nudge = (axis: string, delta: number) =>
    sendOrReport({ type: "manual_jog", robot: robotKey, axis, delta }, "位置の微調整");
  const shiftCenter = (axis: string, current: number, delta: number) =>
    sendOrReport(
      { type: "linkage_center_set", robot: robotKey, axis, center: current + delta },
      "中心の微調整",
    );

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
        {linked.map((axis) => {
          const linkage = axis.linkage;
          if (linkage === null) return null;
          // 今の隙間でずらせる上限。縮めきり (open) では 0 で、閉じたときに効く
          const limit = linkage.limit ?? linkage.max;
          return (
            <div key={`${axis.name}-center`} className="flex items-center gap-1.5">
              <span className="text-[0.85em] text-base-content/70">{axis.name} 中心</span>
              <Button
                disabled={centerBlocked || linkage.center - NUDGE_STEP < -limit}
                aria-label={`${axis.name} の中心を左へ ${NUDGE_STEP}mm`}
                onClick={() => shiftCenter(axis.name, linkage.center, -NUDGE_STEP)}
              >
                ◀ {NUDGE_STEP}
              </Button>
              <span className="min-w-[4.5em] text-center text-[0.85em] tabular-nums">
                {linkage.center > 0 ? "+" : ""}
                {linkage.center.toFixed(1)}mm
              </span>
              <Button
                disabled={centerBlocked || linkage.center + NUDGE_STEP > limit}
                aria-label={`${axis.name} の中心を右へ ${NUDGE_STEP}mm`}
                onClick={() => shiftCenter(axis.name, linkage.center, NUDGE_STEP)}
              >
                {NUDGE_STEP} ▶
              </Button>
              <span className="text-[0.85em] text-base-content/50">±{limit.toFixed(0)}</span>
            </div>
          );
        })}
      </div>
    </Panel>
  );
}
