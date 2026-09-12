import { useEffect, useRef } from "react";

import { ContinuousControls } from "@/components/operator/ContinuousControls";
import { Button } from "@/components/ui/Button";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { useRobotStatus } from "@/context/RobotContext";
import { commandValueText, hasUnit } from "@/lib/commandValue";
import { cx } from "@/lib/cx";
import { isDuringMatch } from "@/lib/phase";
import type { ManualAxis } from "@/lib/protocol";
import { scrollWithinContainer } from "@/lib/scrollWithin";
import { evaluateSync } from "@/lib/syncVerdict";

interface ManualAxisRowProps {
  axis: ManualAxis;
  blocked: boolean;
  selected: boolean;
  onSelect: () => void;
  onJog: (axis: string, delta: number) => void;
  onSet: (axis: string, value: number) => void;
  onMove: (axis: string, position: string) => void;
}

// 単位は軸名の横に 1 回だけ出すので、値そのものには付けない
function format(value: number | null, axis: ManualAxis): string {
  if (value === null) return "—";
  return commandValueText(value, axis.command_mode, 2);
}

function unitOf(axis: ManualAxis): string | null {
  return hasUnit(axis.command_mode) && axis.unit ? axis.unit : null;
}

function withUnit(value: number | null, axis: ManualAxis): string {
  const unit = unitOf(axis);
  const text = format(value, axis);
  return unit ? `${text} ${unit}` : text;
}

export function ManualAxisRow({
  axis,
  blocked,
  selected,
  onSelect,
  onJog,
  onSet,
  onMove,
}: ManualAxisRowProps) {
  const range = axis.manual;
  const disabled = blocked;
  const rowRef = useRef<HTMLDivElement>(null);
  const { matchState } = useRobotStatus();
  const inMatch = isDuringMatch(matchState.phase);

  useEffect(() => {
    if (selected) scrollWithinContainer(rowRef.current);
  }, [selected]);

  const presetOnly = range === null;
  const unit = unitOf(axis);
  const motorNames =
    axis.motors.length === 1 && axis.motors[0] === axis.name ? null : axis.motors.join(" / ");
  const currentText = `現在 ${withUnit(axis.value, axis)}`;
  const targetText = `目標 ${withUnit(axis.target, axis)}`;

  const presetButtons =
    axis.positions.length === 0 ? null : (
      <div className="flex flex-wrap items-center gap-1">
        {axis.positions.map((position) => (
          <Button
            key={position.name}
            disabled={disabled}
            onClick={() => onMove(axis.name, position.name)}
            aria-label={`${axis.name} を ${position.name} へ`}
            title={
              presetOnly || position.value === null ? undefined : `${position.value} ${axis.unit}`
            }
          >
            {position.name}
          </Button>
        ))}
      </div>
    );

  return (
    <div
      ref={rowRef}
      className={cx(
        "flex flex-col gap-1 border-b border-base-300 border-l-[0.4rem] px-2 py-1.5 last:border-b-0",
        selected ? "border-l-info bg-base-200/40" : "border-l-transparent",
      )}
      onPointerDown={onSelect}
      onFocusCapture={onSelect}
    >
      <div
        className={cx(
          "flex min-w-0 flex-wrap gap-x-3 gap-y-0.5",
          presetOnly ? "items-center" : "items-baseline",
        )}
      >
        <span
          className="min-w-0 shrink-0 font-medium"
          title={motorNames === null ? undefined : `モータ ${motorNames}`}
        >
          {axis.name}
          {unit ? (
            <span className="ml-1 text-[0.75em] font-normal text-base-content/45">{unit}</span>
          ) : null}
        </span>
        {/* モータ名は試合中には使わない。セッティング中だけ出す */}
        {motorNames === null || inMatch ? null : (
          <span className="shrink-0 text-[0.8em] text-base-content/45">{motorNames}</span>
        )}

        <SyncIndicator axis={axis} />

        {presetOnly ? presetButtons : null}

        {/* ラベル語を置かず、大きさと上下の位置で現在値と目標値を分ける */}
        <span className="ml-auto flex shrink-0 flex-col items-end font-mono leading-tight tabular-nums">
          {axis.command_mode === "position" ? (
            <>
              <span
                className="text-[1.15em] font-medium"
                title={currentText}
                aria-label={currentText}
              >
                {format(axis.value, axis)}
              </span>
              <span
                className="text-[0.8em] text-base-content/55"
                title={targetText}
                aria-label={targetText}
              >
                {format(axis.target, axis)}
                <Delta value={axis.value} target={axis.target} />
              </span>
            </>
          ) : (
            <span className="text-base-content/70" title={targetText} aria-label={targetText}>
              {format(axis.target, axis)}
              <Delta value={axis.value} target={axis.target} />
            </span>
          )}
        </span>
      </div>

      {range && range.steps.length > 0 ? (
        <ContinuousControls
          axis={axis}
          min={range.min}
          max={range.max}
          steps={range.steps}
          disabled={disabled}
          selected={selected}
          onJog={onJog}
          onSet={onSet}
        />
      ) : null}

      {presetOnly ? null : presetButtons}
    </div>
  );
}

function Delta({ value, target }: { value: number | null; target: number | null }) {
  if (value === null || target === null) return null;
  const delta = target - value;
  if (Math.abs(delta) < 0.005) return null;
  return (
    <span className="ml-1 text-[0.9em] text-base-content/50">
      ({delta > 0 ? "+" : ""}
      {delta.toFixed(2)})
    </span>
  );
}

function SyncIndicator({ axis }: { axis: ManualAxis }) {
  const verdict = evaluateSync(axis);
  const { deviation, sync_tolerance: tolerance } = axis;
  if (typeof deviation !== "number") return null;
  // 正常時の偏差は読む必要がない。異常のときだけ出す
  if (!verdict.alert) return null;

  const text = `ずれ ${deviation.toFixed(2)}${axis.unit ? ` ${axis.unit}` : ""}`;
  const title =
    typeof tolerance === "number" ? `許容差 ${tolerance.toFixed(2)} ${axis.unit}` : undefined;

  return (
    <StatusBadge tone={verdict.tone} className="shrink-0" title={title}>
      {text}
    </StatusBadge>
  );
}
