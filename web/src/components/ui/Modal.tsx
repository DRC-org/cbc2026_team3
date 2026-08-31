import { useEffect, useId, useRef } from "react";
import type { ReactNode } from "react";

import { useModalRegistry } from "@/context/ModalContext";
import { cx } from "@/lib/cx";

export type ModalTone = "default" | "danger" | "estop";

/** Tab で到達しうる要素。`disabled` の除外は取得後に行う */
const FOCUSABLE_SELECTOR =
  'a[href], button, input, select, textarea, [tabindex]:not([tabindex="-1"])';

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
  default: "border-base-300 bg-base-100",
  // 破壊的操作・危険操作の確認ダイアログは枠と見出しを危険色にする
  danger: "border-error bg-base-100",
  // 停止中であることが画面のどこを見ても分かる必要がある唯一の状態。
  // グレー基調の例外として、ここだけは面を赤で塗る
  estop: "border-estop-fg bg-estop text-estop-fg",
};

const TONE_TITLE_CLASS: Record<ModalTone, string> = {
  default: "text-base-content",
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
 * 背景の暗さだけはユーティリティで上書きする（daisyUI 既定は #0006 と薄く、
 * ライト地の上では背後の画面と分離しない）。
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
  const titleId = useId();
  const boxRef = useRef<HTMLDivElement>(null);
  const restoreRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (!open) return;
    return register();
  }, [open, register]);

  /**
   * 開いたら中へフォーカスを入れ、閉じたら元の要素へ戻す。
   *
   * 初期フォーカスは**箱そのもの**で、中のボタンには当てない。当てると
   * 「最初に Enter で押されるもの」がモーダルごとに変わり、確認ダイアログの
   * 既定が「開始」や「Reset」になる並びを作れてしまう。
   */
  useEffect(() => {
    if (!open) return;
    restoreRef.current = document.activeElement as HTMLElement | null;
    boxRef.current?.focus();
    return () => restoreRef.current?.focus?.();
  }, [open]);

  /**
   * Tab を箱の中へ閉じ込める。**`onClose` を持つモーダルだけ。**
   *
   * ホットキーは `ModalContext` で封じてあるが、Tab + Enter は素通りするので
   * 背後の画面の操作へ届いてしまう。一方、緊急停止オーバーレイ (`onClose` 無し)
   * は一時的なダイアログではなく停止状態の表示そのもので、閉じるまでの間ずっと
   * 続く。ここを閉じ込めると停止中は画面の他のどこへもキーボードで到達できなく
   * なるため、トラップは掛けない。**閉じる手段が増えるわけではない**
   * (Tab で抜けてもオーバーレイは開いたまま)。
   */
  useEffect(() => {
    if (!open || !onClose) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Tab") return;
      const box = boxRef.current;
      if (!box) return;

      const focusable = [...box.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)].filter(
        (el) => !el.hasAttribute("disabled") && el.getAttribute("aria-hidden") !== "true",
      );
      if (focusable.length === 0) {
        event.preventDefault();
        box.focus();
        return;
      }

      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const active = document.activeElement;

      if (!box.contains(active)) {
        event.preventDefault();
        (event.shiftKey ? last : first).focus();
      } else if (event.shiftKey && (active === first || active === box)) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && active === last) {
        event.preventDefault();
        first.focus();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [open, onClose]);

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
      className="modal modal-open bg-[rgb(24_27_31_/_62%)]"
      onClick={onClose ? (event) => event.target === event.currentTarget && onClose() : undefined}
      role="presentation"
    >
      <div
        ref={boxRef}
        // 初期フォーカスの受け皿。Tab 順には入れない (-1)
        tabIndex={-1}
        className={cx(
          "modal-box flex max-h-[90vh] flex-col gap-2 border p-3 outline-none",
          TONE_BOX_CLASS[tone],
          boxClassName,
        )}
        role={role}
        aria-modal="true"
        // 読み上げ名は見出しから取る。別名を与えたいときだけ ariaLabel が勝つ
        aria-label={ariaLabel}
        aria-labelledby={ariaLabel ? undefined : titleId}
      >
        <h3
          id={titleId}
          className={cx(
            "shrink-0 border-b border-current/15 pb-1 text-[1.05em] font-bold tracking-wide",
            TONE_TITLE_CLASS[tone],
          )}
        >
          {title}
        </h3>
        <div className={cx("scroll min-h-0 flex-1", bodyClassName)}>{children}</div>
        {footer ? <div className="flex shrink-0 justify-end gap-2 pt-1">{footer}</div> : null}
      </div>
    </div>
  );
}
