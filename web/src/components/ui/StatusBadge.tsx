import type { ReactNode } from "react";

import { cx } from "@/lib/cx";
import type { Tone } from "@/lib/tone";
import { TONE_BADGE_CLASS, TONE_STATUS_CLASS } from "@/lib/tone";

interface StatusBadgeProps {
  tone: Tone;
  children: ReactNode;
  detail?: ReactNode;
  className?: string;
  title?: string;
}

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
