import type { ButtonHTMLAttributes } from "react";

import { cx } from "@/lib/cx";

export type ButtonTone = "default" | "ok" | "warn" | "danger" | "info" | "next" | "estopReset";

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  tone?: ButtonTone;
}

const DISABLED_CLASS =
  "disabled:border-base-300 disabled:bg-base-200 disabled:text-base-content/65 disabled:opacity-100 disabled:shadow-none";

const TONE_CLASS: Record<ButtonTone, string> = {
  default:
    "btn btn-sm border-base-300 bg-base-100 text-base-content hover:border-base-content/30 hover:bg-base-200",
  ok: "btn btn-sm btn-outline btn-success",
  warn: "btn btn-sm btn-outline btn-warning",
  danger: "btn btn-sm btn-outline btn-error",
  info: "btn btn-sm btn-outline btn-info",
  next: "btn btn-sm border-next bg-next text-next-fg hover:border-next hover:bg-next/85",
  estopReset:
    "btn btn-sm border-estop-fg bg-estop-fg text-estop hover:border-white hover:bg-white hover:text-estop",
};

export function Button({ tone = "default", className, ...props }: ButtonProps) {
  return (
    <button
      type="button"
      className={cx(TONE_CLASS[tone], DISABLED_CLASS, "gap-1.5", className)}
      {...props}
    />
  );
}
