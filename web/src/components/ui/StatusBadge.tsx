import type { ReactNode } from "react";

import { cx } from "@/lib/cx";
import type { Tone } from "@/lib/tone";
import { TONE_BADGE_CLASS, TONE_STATUS_CLASS } from "@/lib/tone";

interface StatusBadgeProps {
  tone: Tone;
  children: ReactNode;
  /** 数値など、状態名の後ろに淡く添える補足 */
  detail?: ReactNode;
  className?: string;
  title?: string;
}

/**
 * 状態表示の唯一の形。
 *
 * 暗色時代は `[✓ OK]` のような着色テキストで状態を示していたが、ライト地では
 * 白背景に載る警告色を AA (4.5:1) まで暗くする必要があり、そこまで落とすと
 * もはや警告色に見えない。地・枠・文字の 3 点で示す淡色チップに置き換えることで、
 * 文字色の明度を犠牲にせず状態を目立たせる。
 *
 * 角丸は 0 のままなので、見た目は丸ピルではなく矩形タグ + 正方形 LED になる。
 */
export function StatusBadge({ tone, children, detail, className, title }: StatusBadgeProps) {
  return (
    <span
      className={cx(
        TONE_BADGE_CLASS[tone],
        "badge-sm max-w-full gap-1.5 whitespace-nowrap",
        className,
      )}
      title={title}
    >
      <span className={TONE_STATUS_CLASS[tone]} />
      <span className="min-w-0 truncate">{children}</span>
      {detail ? <span className="shrink-0 opacity-75">{detail}</span> : null}
    </span>
  );
}
