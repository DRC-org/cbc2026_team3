import { useEffect } from "react";
import type { ReactNode } from "react";

import { useModalRegistry } from "@/context/ModalContext";
import { cx } from "@/lib/cx";

export type ModalTone = "default" | "danger" | "estop";

interface ModalProps {
  open: boolean;
  /**
   * 省略すると背景クリックでも Esc でも閉じられなくなる。
   * 緊急停止オーバーレイのように解除経路を特定のボタンだけに限定したい場合に使う。
   */
  onClose?: () => void;
  title: ReactNode;
  tone?: ModalTone;
  role?: "dialog" | "alertdialog";
  /** タイトルと別の読み上げ名を与えたい場合のみ指定する */
  ariaLabel?: string;
  bodyClassName?: string;
  boxClassName?: string;
  footer?: ReactNode;
  children: ReactNode;
}

const TONE_BOX_CLASS: Record<ModalTone, string> = {
  default: "border-fg-dim bg-raised",
  // 破壊的操作・危険操作の確認ダイアログは枠と見出しを危険色にする
  danger: "border-error bg-raised",
  // 停止中であることが画面のどこを見ても分かる必要がある唯一の状態。
  // グレー基調の例外として、ここだけは面を赤で塗る
  estop: "border-estop-fg bg-estop text-estop-fg",
};

const TONE_TITLE_CLASS: Record<ModalTone, string> = {
  default: "text-fg-strong",
  danger: "text-error",
  estop: "text-estop-fg",
};

/**
 * daisyUI の modal を `<div>` 版で使うモーダル。
 *
 * `<dialog>` + showModal() は Esc で必ず閉じてしまい、解除経路を限定したい
 * 緊急停止オーバーレイの要件と両立しない。閉じる手段は onClose の有無だけで
 * 決まる形にして、渡さなければ構造的に閉じられないことを保証する。
 *
 * 外枠の `modal modal-open` は必須。`.modal-box` は既定が `opacity:0; scale:.95` で、
 * 可視化するルールは `.modal-open` 側にしかない。付け忘れると Tailwind の
 * ツリーシェイクでそのルールごと CSS から消え、モーダルが不可視のまま出荷される。
 * 背景の暗さだけはユーティリティで上書きする（daisyUI 既定は #0006 と薄い）。
 */
export function Modal({
  open,
  onClose,
  title,
  tone = "default",
  role = "dialog",
  ariaLabel,
  bodyClassName,
  boxClassName,
  footer,
  children,
}: ModalProps) {
  const { register } = useModalRegistry();

  useEffect(() => {
    if (!open) return;
    return register();
  }, [open, register]);

  useEffect(() => {
    if (!open || !onClose) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div
      className="modal modal-open bg-[rgb(8_9_11_/_78%)]"
      onClick={onClose ? (event) => event.target === event.currentTarget && onClose() : undefined}
      role="presentation"
    >
      <div
        className={cx(
          "modal-box flex max-h-[90vh] flex-col gap-2 border",
          TONE_BOX_CLASS[tone],
          boxClassName,
        )}
        role={role}
        aria-modal="true"
        aria-label={ariaLabel}
      >
        <h3 className={cx("shrink-0 font-bold", TONE_TITLE_CLASS[tone])}>{title}</h3>
        <div className={cx("scroll min-h-0 flex-1", bodyClassName)}>{children}</div>
        {footer ? <div className="flex shrink-0 justify-end gap-2 pt-2">{footer}</div> : null}
      </div>
    </div>
  );
}
