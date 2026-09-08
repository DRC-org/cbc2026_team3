import type { ReactNode } from "react";

import { cx } from "@/lib/cx";

export function Kbd({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <kbd className={cx("kbd kbd-xs bg-base-200 font-mono text-base-content/70", className)}>
      {children}
    </kbd>
  );
}
