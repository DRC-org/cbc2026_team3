import type { LucideIcon, LucideProps } from "lucide-react";

import { cx } from "@/lib/cx";

interface IconProps extends Omit<LucideProps, "ref"> {
  as: LucideIcon;
}

export function Icon({ as: Glyph, className, ...rest }: IconProps) {
  return (
    <Glyph
      size="1em"
      strokeWidth={1.75}
      aria-hidden
      className={cx("inline-block shrink-0", className)}
      {...rest}
    />
  );
}
