import type { ReactNode } from "react";

import { cx } from "@/lib/cx";
import type { Tone } from "@/lib/tone";
import { TONE_BORDER_L_CLASS } from "@/lib/tone";

interface PanelProps {
  legend?: ReactNode;
  actions?: ReactNode;
  accentTone?: Tone;
  className?: string;
  bodyClassName?: string;
  children: ReactNode;
}

const ACCENT_WIDTH_CLASS = "border-l-[0.4rem]";

export function Panel({
  legend,
  actions,
  accentTone,
  className,
  bodyClassName,
  children,
}: PanelProps) {
  return (
    <section
      className={cx(
        "card card-border flex min-h-0 min-w-0 flex-col border-base-300 bg-base-100",
        accentTone && ACCENT_WIDTH_CLASS,
        accentTone && TONE_BORDER_L_CLASS[accentTone],
        className,
      )}
    >
      {legend === undefined ? null : (
        <div className="flex shrink-0 items-center justify-between gap-2 border-b border-base-300 px-2 py-[0.15rem]">
          <h2 className="min-w-0 truncate text-[0.82em] font-medium tracking-wide text-base-content/70">
            {legend}
          </h2>
          {actions ? <div className="flex shrink-0 items-center gap-1">{actions}</div> : null}
        </div>
      )}
      <div className={cx("flex min-h-0 flex-1 flex-col p-2", bodyClassName)}>{children}</div>
    </section>
  );
}
