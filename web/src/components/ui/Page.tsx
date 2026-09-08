import type { ReactNode } from "react";

import { cx } from "@/lib/cx";

export function Page({ className, children }: { className?: string; children: ReactNode }) {
  return (
    <main className={cx("min-h-0 flex-1 gap-2 overflow-hidden p-2", className)}>{children}</main>
  );
}
