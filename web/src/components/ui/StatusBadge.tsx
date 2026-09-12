import type { ReactNode } from "react";

import { cx } from "@/lib/cx";
import type { Tone } from "@/lib/tone";
import { TONE_BADGE_CLASS, TONE_STATUS_CLASS } from "@/lib/tone";

interface StatusBadgeProps {
  tone: Tone;
  children?: ReactNode;
  detail?: ReactNode;
  className?: string;
  title?: string;
}

export function StatusBadge({ tone, children, detail, className, title }: StatusBadgeProps) {
  const bare = children === null || children === undefined || children === false;

  return (
    <span
      className={cx(
        TONE_BADGE_CLASS[tone],
        "badge-sm max-w-full whitespace-nowrap",
        bare && !detail ? "px-1" : "gap-1.5",
        className,
      )}
      title={title}
    >
      <span className={TONE_STATUS_CLASS[tone]} />
      {bare ? (
        // ドットだけのときは色が唯一の手がかりになるので、読み上げ用に文字を残す
        title ? (
          <span className="sr-only">{title}</span>
        ) : null
      ) : (
        <span className="min-w-0 truncate">{children}</span>
      )}
      {detail ? <span className="shrink-0 opacity-75">{detail}</span> : null}
    </span>
  );
}
