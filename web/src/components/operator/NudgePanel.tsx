import { useState } from "react";

import { Button } from "@/components/ui/Button";
import { Panel } from "@/components/ui/Panel";
import { StatusBadge } from "@/components/ui/StatusBadge";
import type { RobotCommands } from "@/context/RobotContext";
import { cx } from "@/lib/cx";
import type { ManualAxis, ManualState } from "@/lib/protocol";

// 箱の上で位置を詰めるための刻み。既定は 2 (大きいと 1 押しで箱を跨ぐ)
const NUDGE_STEPS = [1, 2, 5] as const;
const DEFAULT_STEP = 2;

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

const fmt = (value: number | null) => (value === null ? "—" : value.toFixed(1));
const signed = (value: number) => `${value > 0 ? "+" : ""}${value.toFixed(1)}`;

/** 半自動で止まっているあいだの位置の微調整。向きの言葉は yaml (manual.labels) が持つ。 */
export function NudgePanel({
  robotKey,
  manual,
  blocked,
  blockedReason,
  centerBlocked,
  sendOrReport,
}: NudgePanelProps) {
  const [step, setStep] = useState<number>(DEFAULT_STEP);
  // 連続値を送れる軸だけ (サーバーが `manual` を配る)。常時操作の軸は別パネルが持つ
  const axes = manual.axes.filter((axis) => axis.manual !== null && axis.manual_always !== true);
  // リンク機構の軸は中心を左右へずらせる (サーバーが `linkage` を配る)
  const linked = manual.axes.filter((axis) => axis.linkage !== null);
  if (axes.length === 0 && linked.length === 0) return null;

  const nudge = (axis: string, delta: number) =>
    sendOrReport({ type: "manual_jog", robot: robotKey, axis, delta }, "位置の微調整");
  const returnTo = (axis: string, value: number) =>
    sendOrReport({ type: "manual_set", robot: robotKey, axis, value }, "基準へ戻す");
  const shiftCenter = (axis: string, current: number, delta: number) =>
    sendOrReport(
      { type: "linkage_center_set", robot: robotKey, axis, center: current + delta },
      "中心の微調整",
    );

  const stepToggle = (
    <div className="join" role="group" aria-label="微調整の刻み">
      {NUDGE_STEPS.map((candidate) => (
        <Button
          key={candidate}
          className={cx(
            "join-item btn-sm min-w-[2.2em]",
            candidate === step && "btn-active border-neutral bg-neutral text-neutral-content",
          )}
          aria-pressed={candidate === step}
          aria-label={`刻みを ${candidate}mm にする`}
          onClick={() => setStep(candidate)}
        >
          {candidate}
        </Button>
      ))}
    </div>
  );

  return (
    <Panel
      legend="位置の微調整"
      className="shrink-0"
      actions={
        <span className="flex items-center gap-2">
          {blockedReason ? <StatusBadge tone="error">{blockedReason}</StatusBadge> : null}
          {stepToggle}
          <span className="text-[0.85em] text-base-content/50">mm</span>
        </span>
      }
    >
      <div className="flex flex-col gap-1.5">
        {axes.map((axis) => (
          <NudgeRow
            key={axis.name}
            axis={axis}
            step={step}
            blocked={blocked}
            onNudge={nudge}
            onReturn={returnTo}
          />
        ))}
        {linked.map((axis) => {
          const linkage = axis.linkage;
          if (linkage === null) return null;
          // 今の隙間でずらせる上限。縮めきり (open) では 0 で、閉じたときに効く
          const limit = linkage.limit ?? linkage.max;
          return (
            <div key={`${axis.name}-center`} className="flex items-center gap-2">
              <Button
                className="min-h-[2.4em] min-w-[5.5em] text-[1.05em]"
                disabled={centerBlocked || linkage.center - step < -limit}
                aria-label={`${axis.name} の中心を左へ ${step}mm`}
                onClick={() => shiftCenter(axis.name, linkage.center, -step)}
              >
                ◀ 左 {step}
              </Button>
              <span className="flex min-w-[10em] flex-col items-center leading-tight">
                <span className="text-[0.8em] text-base-content/60">{axis.name} 中心</span>
                <span className="tabular-nums">
                  {signed(linkage.center)}mm
                  <span className="ml-1 text-[0.8em] text-base-content/50">
                    ±{limit.toFixed(0)}
                  </span>
                </span>
              </span>
              <Button
                className="min-h-[2.4em] min-w-[5.5em] text-[1.05em]"
                disabled={centerBlocked || linkage.center + step > limit}
                aria-label={`${axis.name} の中心を右へ ${step}mm`}
                onClick={() => shiftCenter(axis.name, linkage.center, step)}
              >
                右 {step} ▶
              </Button>
            </div>
          );
        })}
      </div>
    </Panel>
  );
}

interface NudgeRowProps {
  axis: ManualAxis;
  step: number;
  blocked: boolean;
  onNudge: (axis: string, delta: number) => void;
  onReturn: (axis: string, value: number) => void;
}

function NudgeRow({ axis, step, blocked, onNudge, onReturn }: NudgeRowProps) {
  const labels = axis.manual?.labels ?? null;
  const minusLabel = labels === null ? `− ${step}` : `${labels.minus} ${step}`;
  const plusLabel = labels === null ? `+ ${step}` : `${labels.plus} ${step}`;
  // 基準 (このステップで最初に押す前の目標) からの累計。押しすぎたら一発で戻れる
  const offset =
    axis.baseline === null || axis.target === null ? null : axis.target - axis.baseline;
  const moved = offset !== null && Math.abs(offset) >= 0.05;

  return (
    <div className="flex items-center gap-2">
      <Button
        className="min-h-[2.4em] min-w-[5.5em] text-[1.05em]"
        disabled={blocked}
        aria-label={`${axis.name} を ${step}${axis.unit} 戻す`}
        onClick={() => onNudge(axis.name, -step)}
      >
        {minusLabel}
      </Button>
      <span className="flex min-w-[10em] flex-col items-center leading-tight">
        <span className="text-[0.8em] text-base-content/60">{axis.name}</span>
        <span className="tabular-nums">
          {fmt(axis.target)}
          <span className="ml-0.5 text-[0.8em] text-base-content/50">{axis.unit}</span>
          {offset === null ? null : (
            <span className={cx("ml-1.5", moved ? "text-warning" : "text-base-content/50")}>
              ({signed(offset)})
            </span>
          )}
        </span>
      </span>
      <Button
        className="min-h-[2.4em] min-w-[5.5em] text-[1.05em]"
        disabled={blocked}
        aria-label={`${axis.name} を ${step}${axis.unit} 進める`}
        onClick={() => onNudge(axis.name, step)}
      >
        {plusLabel}
      </Button>
      {axis.baseline !== null && moved ? (
        <Button
          className="btn-sm"
          disabled={blocked}
          aria-label={`${axis.name} を基準 ${fmt(axis.baseline)}${axis.unit} へ戻す`}
          onClick={() => onReturn(axis.name, axis.baseline as number)}
        >
          基準へ戻す
        </Button>
      ) : null}
    </div>
  );
}
