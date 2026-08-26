import type { LucideIcon, LucideProps } from "lucide-react";

import { cx } from "@/lib/cx";

interface IconProps extends Omit<LucideProps, "ref"> {
  as: LucideIcon;
}

/**
 * lucide アイコンの既定値をここ 1 箇所に閉じ込める。
 *
 * `size="1em"` で文字サイズに追従させるのは、ルートの `clamp()` スケーリングに
 * アイコンだけ取り残されないようにするため（px 固定だと小さい画面で相対的に巨大になる）。
 * `shrink-0` は flex 行でアイコンが潰れる事故を防ぐ。
 *
 * `absoluteStrokeWidth` は使わない。lucide 内部で `strokeWidth * 24 / Number(size)` を
 * 計算するため、`size` が "1em" のような文字列だと NaN になり線が消える。
 */
export function Icon({ as: Glyph, className, ...rest }: IconProps) {
  return (
    <Glyph
      size="1em"
      strokeWidth={1.75}
      aria-hidden
      className={cx("inline-block shrink-0", className)}
      {...rest}
    />
  );
}
