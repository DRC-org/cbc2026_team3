import type { ButtonHTMLAttributes } from "react";

import { cx } from "@/lib/cx";

export type ButtonTone = "default" | "ok" | "warn" | "danger" | "info" | "next" | "estopReset";

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  tone?: ButtonTone;
  /** 現在選択中のトグル（モード / コート）であることを反転で示す */
  selected?: boolean;
}

/**
 * 主要操作のトーン。
 *
 * 既定と ok/warn/danger/info は地をベタ塗りせず縁と文字色で示す。
 * ボタンが並んでも画面が原色で埋まらないようにするため
 * （daisyUI の btn-outline がホバー時だけ面を塗る挙動と一致する）。
 * next だけは例外で、試合中に最も多く押すため周辺視野でも分かるよう面で主張させる。
 */
/**
 * 無効時の見た目。daisyUI の既定は文字が base-content の 20%・枠が透明で、
 * 「⊘ 準備中」「RUNNING」「✓ DONE」のように*状態表示を兼ねる*無効ボタンが読めない。
 * 押せないことは示しつつ、文字と枠は視認できる濃さに戻す。
 */
const DISABLED_CLASS =
  "disabled:border-line-soft disabled:bg-transparent disabled:text-fg-dim disabled:opacity-60";

const TONE_CLASS: Record<ButtonTone, string> = {
  default: "btn btn-sm border-line bg-transparent hover:border-fg-dim hover:bg-raised",
  ok: "btn btn-sm btn-outline btn-success",
  warn: "btn btn-sm btn-outline btn-warning",
  danger: "btn btn-sm btn-outline btn-error",
  info: "btn btn-sm btn-outline btn-info",
  next: "btn btn-sm btn-warning",
  // 赤い面の上に置くため、通常のボタン配色では読めない
  estopReset:
    "btn btn-sm border-estop-fg bg-estop-fg text-estop hover:border-white hover:bg-white hover:text-estop",
};

export function Button({ tone = "default", selected = false, className, ...props }: ButtonProps) {
  return (
    <button
      type="button"
      className={cx(
        TONE_CLASS[tone],
        DISABLED_CLASS,
        selected && "border-fg-dim bg-raised text-fg-strong",
        className,
      )}
      {...props}
    />
  );
}
