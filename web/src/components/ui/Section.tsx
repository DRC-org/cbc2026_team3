import type { ReactNode } from "react";

import { cx } from "@/lib/cx";

interface SectionProps {
  title?: ReactNode;
  className?: string;
  children: ReactNode;
}

/**
 * パネル内の区切り。入れ子のパネルを作らず、罫線 1 本 + 小見出しでグループを表す。
 *
 * 先頭の Section だけ上罫線と余白を落とすことで、パネル見出しの直下に
 * 意味のない二重線が出るのを防ぐ。
 */
export function Section({ title, className, children }: SectionProps) {
  return (
    <section
      className={cx(
        "mt-1.5 flex shrink-0 flex-col gap-1 border-t border-base-300 pt-1.5 first:mt-0 first:border-t-0 first:pt-0",
        className,
      )}
    >
      {title === undefined ? null : (
        <div className="flex items-baseline justify-between gap-3 text-[0.8em] tracking-wide text-base-content/70">
          <span className="min-w-0 truncate">{title}</span>
        </div>
      )}
      {children}
    </section>
  );
}
