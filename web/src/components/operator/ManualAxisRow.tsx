import { useEffect, useRef } from "react";

import { ContinuousControls } from "@/components/operator/ContinuousControls";
import { Button } from "@/components/ui/Button";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { cx } from "@/lib/cx";
import type { ManualAxis } from "@/lib/protocol";
import { evaluateSync } from "@/lib/syncVerdict";

interface ManualAxisRowProps {
  axis: ManualAxis;
  blockedReason: string | null;
  selected: boolean;
  onSelect: () => void;
  onJog: (axis: string, delta: number) => void;
  onSet: (axis: string, value: number) => void;
  onMove: (axis: string, position: string) => void;
}

function format(value: number | null, unit: string): string {
  return value === null ? "—" : `${value.toFixed(2)}${unit ? ` ${unit}` : ""}`;
}

export function ManualAxisRow({
  axis,
  blockedReason,
  selected,
  onSelect,
  onJog,
  onSet,
  onMove,
}: ManualAxisRowProps) {
  const range = axis.manual;
  const disabled = blockedReason !== null;
  const rowRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (selected) rowRef.current?.scrollIntoView?.({ block: "nearest" });
  }, [selected]);

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
      <div className="flex min-w-0 flex-wrap items-baseline gap-x-3 gap-y-0.5">
        <span className="min-w-0 shrink-0 font-medium">{axis.name}</span>
        {axis.motors.length === 1 && axis.motors[0] === axis.name ? null : (
          <span className="shrink-0 text-[0.8em] text-base-content/45">
            {axis.motors.join(" / ")}
          </span>
        )}

        <SyncIndicator axis={axis} />

        <span className="ml-auto flex shrink-0 items-baseline gap-3 font-mono tabular-nums">
          <span className="text-[1.15em] font-medium">
            <span className="mr-1 font-sans text-[0.7em] font-normal text-base-content/55">
              現在
            </span>
            {format(axis.value, axis.unit)}
          </span>
          <span className="text-base-content/70">
            <span className="mr-1 font-sans text-[0.8em] text-base-content/55">目標</span>
            {format(axis.target, axis.unit)}
            <Delta value={axis.value} target={axis.target} />
          </span>
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

      {axis.positions.length > 0 ? (
        <div className="flex flex-wrap items-center gap-1">
          {axis.positions.map((position) => (
            <Button
              key={position}
              disabled={disabled}
              onClick={() => onMove(axis.name, position)}
              aria-label={`${axis.name} を ${position} へ`}
            >
              {position}
            </Button>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function Delta({ value, target }: { value: number | null; target: number | null }) {
  if (value === null || target === null) return null;
  const delta = target - value;
  if (Math.abs(delta) < 0.005) return null;
  return (
    <span className="ml-1 text-[0.8em] text-base-content/50">
      ({delta > 0 ? "+" : ""}
      {delta.toFixed(2)})
    </span>
  );
}

function SyncIndicator({ axis }: { axis: ManualAxis }) {
  const verdict = evaluateSync(axis);
  const { deviation, sync_tolerance: tolerance } = axis;
  if (typeof deviation !== "number") return null;

  const text = `ずれ ${deviation.toFixed(2)}${axis.unit ? ` ${axis.unit}` : ""}`;
  const title =
    typeof tolerance === "number" ? `許容差 ${tolerance.toFixed(2)} ${axis.unit}` : undefined;

  if (!verdict.alert) {
    return (
      <span
        className="shrink-0 font-mono text-[0.8em] text-base-content/45 tabular-nums"
        title={title}
      >
        {text}
      </span>
    );
  }
  return (
    <StatusBadge tone={verdict.tone} className="shrink-0" title={title}>
      {text}
    </StatusBadge>
  );
}
