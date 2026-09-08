import type { ManualAxis } from "@/lib/protocol";

function ratio(value: number, min: number, max: number): number {
  return Math.min(100, Math.max(0, ((value - min) / (max - min)) * 100));
}

export function RangeBar({ axis, min, max }: { axis: ManualAxis; min: number; max: number }) {
  const valuePct = axis.value === null ? null : ratio(axis.value, min, max);
  const targetPct = axis.target === null ? null : ratio(axis.target, min, max);

  return (
    <div className="flex items-center gap-2 text-[0.8em] text-base-content/70">
      <span className="w-16 shrink-0 text-right font-mono tabular-nums">
        {min} {axis.unit}
      </span>
      <div className="relative h-2.5 min-w-0 flex-1 border border-base-300 bg-base-200">
        {axis.positions.map((position) =>
          position.value === null ? null : (
            <span
              key={position.name}
              className="absolute bottom-0 h-1 w-px -translate-x-1/2 bg-base-content/40"
              style={{ left: `${ratio(position.value, min, max)}%` }}
              title={`${position.name} ${position.value} ${axis.unit}`}
              aria-hidden
            />
          ),
        )}

        {valuePct === null || targetPct === null ? null : (
          <span
            className="absolute top-0 h-full bg-info/15"
            style={{
              left: `${Math.min(valuePct, targetPct)}%`,
              width: `${Math.abs(targetPct - valuePct)}%`,
            }}
            aria-hidden
          />
        )}
        {targetPct === null ? null : (
          <span
            className="absolute top-0 h-full w-1 -translate-x-1/2 bg-info/70"
            style={{ left: `${targetPct}%` }}
            aria-hidden
          />
        )}
        {valuePct === null ? null : (
          <span
            className="absolute top-0 h-full w-[0.1875rem] -translate-x-1/2 bg-base-content"
            style={{ left: `${valuePct}%` }}
            aria-hidden
          />
        )}
      </div>
      <span className="w-16 shrink-0 font-mono tabular-nums">
        {max} {axis.unit}
      </span>
    </div>
  );
}
