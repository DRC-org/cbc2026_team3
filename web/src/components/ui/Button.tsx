import type { ButtonHTMLAttributes } from "react";

import { cx } from "@/lib/cx";

export type ButtonTone = "default" | "ok" | "warn" | "danger" | "info" | "next" | "estopReset";

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  tone?: ButtonTone;
  /** 現在選択中のトグル（モード / コート）であることを反転で示す */
  selected?: boolean;
}

/**
 * 無効時の見た目。daisyUI の既定は地 base-content 10% / 文字 base-content 20% で、
 * 白地の上ではほぼ消える。`⊘ 準備中` `RUNNING` `✓ DONE` のように*状態表示を兼ねる*
 * 無効ボタンが読めなくなるため、押せないことは示しつつ文字は AA を保つ濃さに戻す。
 */
const DISABLED_CLASS =
  "disabled:border-base-300 disabled:bg-base-200 disabled:text-base-content/65 disabled:opacity-100 disabled:shadow-none";

/**
 * 主要操作のトーン。
 *
 * 既定と ok/warn/danger/info は地をベタ塗りせず縁と文字色で示す。
 * ボタンが並んでも画面が原色で埋まらないようにするため
 * （daisyUI の btn-outline がホバー時だけ面を塗る挙動と一致する）。
 *
 * next だけは例外で面を塗る。試合中に最も多く押すため周辺視野でも分かる必要があり、
 * 状態色の warning は「白地に載る文字」として読める明度まで落としてあるので
 * 面塗りには暗すぎる。専用の明るい amber (--color-next) を当てる。
 */
const TONE_CLASS: Record<ButtonTone, string> = {
  default:
    "btn btn-sm border-base-300 bg-base-100 text-base-content hover:border-base-content/30 hover:bg-base-200",
  ok: "btn btn-sm btn-outline btn-success",
  warn: "btn btn-sm btn-outline btn-warning",
  danger: "btn btn-sm btn-outline btn-error",
  info: "btn btn-sm btn-outline btn-info",
  next: "btn btn-sm border-next bg-next text-next-fg hover:border-next hover:bg-next/85",
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
        "gap-1.5",
        selected && "border-base-content/40 bg-base-200 text-base-content",
        className,
      )}
      {...props}
    />
  );
}
