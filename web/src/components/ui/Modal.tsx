import { useEffect, useId, useRef } from "react";
import type { ReactNode } from "react";

import { useModalRegistry } from "@/context/ModalContext";
import { cx } from "@/lib/cx";

export type ModalTone = "default" | "danger" | "estop";

const FOCUSABLE_SELECTOR =
  'a[href], button, input, select, textarea, [tabindex]:not([tabindex="-1"])';

interface ModalProps {
  open: boolean;
  onClose?: () => void;
  title: ReactNode;
  tone?: ModalTone;
  role?: "dialog" | "alertdialog";
  ariaLabel?: string;
  bodyClassName?: string;
  boxClassName?: string;
  footer?: ReactNode;
  children: ReactNode;
}

const TONE_BOX_CLASS: Record<ModalTone, string> = {
  default: "border-base-300 bg-base-100",
  danger: "border-error bg-base-100",
  estop: "border-estop-fg bg-estop text-estop-fg",
};

const TONE_TITLE_CLASS: Record<ModalTone, string> = {
  default: "text-base-content",
  danger: "text-error",
  estop: "text-estop-fg",
};

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

  useEffect(() => {
    if (!open) return;
    restoreRef.current = document.activeElement as HTMLElement | null;
    boxRef.current?.focus();
    return () => restoreRef.current?.focus?.();
  }, [open]);

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
      // daisyUI 既定の backdrop は #0006。ライト地では薄いので自前で上書きする。
      className="modal modal-open bg-[rgb(24_27_31_/_62%)]"
      onClick={onClose ? (event) => event.target === event.currentTarget && onClose() : undefined}
      role="presentation"
    >
      <div
        ref={boxRef}
        tabIndex={-1}
        className={cx(
          "modal-box flex max-h-[90vh] flex-col gap-2 border p-3 outline-none",
          TONE_BOX_CLASS[tone],
          boxClassName,
        )}
        role={role}
        aria-modal="true"
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
