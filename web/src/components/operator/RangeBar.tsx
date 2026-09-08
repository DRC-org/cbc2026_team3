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
 *
 * **軌道 (地) には必ず輪郭を与える。** 地の `base-200` (#eef1f4) はパネル面の
 * `base-100` (#ffffff) との差が小さく、輪郭が無いと可動範囲そのものが画面に
 * 現れない —— 残るのは現在値の線 1 本だけで、その線が範囲のどこに居るのかを
 * 読む手がかりが消える。目盛りの端 (下限・上限の値) も同じ理由で薄くしすぎない。
 *
 * **プリセットは下端の短い刻みで、現在値・目標値より必ず弱く描く。** 下に並ぶ
 * ボタン (`home` / `extended` / ...) が可動範囲のどこを指すのかは、それまで
 * 画面のどこにも出ていなかった。ただし全高の線で描くと 3 種類の縦線が競り合い、
 * 直す前の「線 1 本」より読めなくなる。**名前をバーへ書き込まない** —— 4 つ
 * 並ぶ軸では必ず重なるので、名前と値の対応はボタン側の `title` が持つ。
 */
export function RangeBar({ axis, min, max }: { axis: ManualAxis; min: number; max: number }) {
  const valuePct = axis.value === null ? null : ratio(axis.value, min, max);
  const targetPct = axis.target === null ? null : ratio(axis.target, min, max);

  return (
    <div className="flex items-center gap-2 text-[0.8em] text-base-content/70">
      <span className="w-16 shrink-0 text-right font-mono tabular-nums">
        {min} {axis.unit}
      </span>
      <div className="relative h-2.5 min-w-0 flex-1 border border-base-300 bg-base-200">
        {/* プリセットの位置。値を引けなかったものは描かない (0 に寄せない) */}
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
