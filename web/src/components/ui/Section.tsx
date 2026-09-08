import type { ReactNode } from "react";

import { cx } from "@/lib/cx";

interface SectionProps {
  title?: ReactNode;
  aside?: ReactNode;
  className?: string;
  children: ReactNode;
}

export function Section({ title, aside, className, children }: SectionProps) {
  return (
    <section
      className={cx(
        "mt-1.5 flex shrink-0 flex-col gap-1 border-t border-base-300 pt-1.5 first:mt-0 first:border-t-0 first:pt-0",
        className,
      )}
    >
      {title === undefined && aside === undefined ? null : (
        <div className="flex items-baseline justify-between gap-3 text-[0.8em] tracking-wide text-base-content/70">
          <span className="min-w-0 truncate">{title}</span>
          {aside === undefined ? null : <span className="shrink-0">{aside}</span>}
        </div>
      )}
      {children}
    </section>
  );
}
