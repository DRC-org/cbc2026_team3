import type { ManualAxis } from "@/lib/protocol";

/** 現在値の可動範囲内での位置 [%]。範囲外は端で止める */
function ratio(value: number, min: number, max: number): number {
  return Math.min(100, Math.max(0, ((value - min) / (max - min)) * 100));
}

/**
 * 可動範囲の表示。**ドラッグできる入力にしてはならない** ——
 * スライダーにすると、掴んだ瞬間に機体がその位置へ飛ぶ。
 *
 * 現在値と目標値は太さと色で描き分ける。同じ 2px の線で描いていた頃は、
 * 両者が近いと重なって「追従が遅れているのか、目標がそこなのか」が読めなかった。
 */
export function RangeBar({ axis, min, max }: { axis: ManualAxis; min: number; max: number }) {
  const valuePct = axis.value === null ? null : ratio(axis.value, min, max);
  const targetPct = axis.target === null ? null : ratio(axis.target, min, max);

  return (
    <div className="flex items-center gap-2 text-[0.8em] text-base-content/55">
      <span className="w-16 shrink-0 text-right font-mono tabular-nums">
        {min} {axis.unit}
      </span>
      <div className="relative h-2.5 min-w-0 flex-1 bg-base-200">
        {/* 現在値から目標値までの移動ぶん。どちらへ向かっているかを面で示す */}
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
            className="absolute top-0 h-full w-0.5 -translate-x-1/2 bg-base-content/80"
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
