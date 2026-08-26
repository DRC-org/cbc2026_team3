import type { ReactNode } from "react";

import { cx } from "@/lib/cx";

/**
 * 全ページ共通の外枠。
 *
 * `display` を敢えて持たない。ページごとに flex / grid が分かれるため、
 * ここで `flex` を敷くと呼び出し側の `grid` と同じ display ユーティリティ同士が
 * 衝突し、どちらが勝つかが Tailwind の出力順という不安定な要因に依存してしまう。
 * 呼び出し側が必ず `flex flex-col` か `grid ...` を指定する。
 *
 * `overflow-hidden` + `min-h-0` はページ全体をスクロールさせないための不変条件。
 * スクロールしてよいのは Panel の本文スロットだけ。
 */
export function Page({ className, children }: { className?: string; children: ReactNode }) {
  return (
    <main className={cx("min-h-0 flex-1 gap-2 overflow-hidden p-2", className)}>{children}</main>
  );
}
