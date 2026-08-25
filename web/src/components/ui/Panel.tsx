import type { ReactNode } from "react";

import { cx } from "@/lib/cx";

interface PanelProps {
  legend?: ReactNode;
  className?: string;
  children: ReactNode;
}

/**
 * 画面上の枠を持つ唯一の単位。
 * 枠は「パネル 1 つにつき 1 本」で入れ子にしない（内部の区切りは .group が担う）。
 */
export function Panel({ legend, className, children }: PanelProps) {
  return (
    <fieldset className={cx("panel", className)}>
      {legend === undefined ? null : <legend>{legend}</legend>}
      {children}
    </fieldset>
  );
}
