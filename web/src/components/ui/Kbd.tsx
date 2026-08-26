import type { ReactNode } from "react";

import { cx } from "@/lib/cx";

/**
 * キーヒント。daisyUI の `kbd` をそのまま使う。
 * ボタン内に置くことが多いため、親の文字色を継がず常に地の文の色で描く。
 */
export function Kbd({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <kbd className={cx("kbd kbd-xs bg-base-200 font-mono text-base-content/70", className)}>
      {children}
    </kbd>
  );
}
