import { OctagonAlert, TriangleAlert, X } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { Icon } from "@/components/ui/Icon";
import { useRobotCommands, useRobotStatus } from "@/context/RobotContext";
import { cx } from "@/lib/cx";
import type { Tone } from "@/lib/tone";
import { TONE_ALERT_CLASS } from "@/lib/tone";

const REJECTION_TTL_MS = 5000;
const HEALTH_TTL_MS = 6000;
const MAX_TOASTS = 3;

type ToastTone = Extract<Tone, "warning" | "error">;

interface ToastItem {
  id: number;
  tone: ToastTone;
  title: string;
  lines: string[];
  expiresAt: number;
}

const TOAST_ICON = {
  warning: TriangleAlert,
  error: OctagonAlert,
} as const;

function ToastCard({ toast, onDismiss }: { toast: ToastItem; onDismiss: () => void }) {
  return (
    <div
      role="alert"
      className={cx(
        TONE_ALERT_CLASS[toast.tone],
        "pointer-events-auto w-[22rem] max-w-[calc(100vw-2rem)] items-start gap-2 p-2",
      )}
    >
      <Icon as={TOAST_ICON[toast.tone]} className="mt-[0.15em] text-[1.1em]" />
      {/* daisyUI の .alert は justify-items:start で子を列幅まで伸ばさない。w-full が無いと
          本文は min-content まで広がり、カードの外へ文字が出る */}
      <div className="w-full min-w-0 wrap-anywhere">
        <div className="font-bold">{toast.title}</div>
        {toast.lines.map((line) => (
          <div key={line} className="opacity-90">
            {line}
          </div>
        ))}
      </div>
      <button
        type="button"
        onClick={onDismiss}
        aria-label="通知を閉じる"
        className="shrink-0 cursor-pointer opacity-70 hover:opacity-100"
      >
        <Icon as={X} className="text-[1.1em]" />
      </button>
    </div>
  );
}

export function Toaster() {
  const { rejection, healthEvents } = useRobotStatus();
  const { clearRejection } = useRobotCommands();
  const [toasts, setToasts] = useState<ToastItem[]>([]);

  const push = useCallback((toast: ToastItem) => {
    setToasts((prev) => {
      if (prev.some((t) => t.id === toast.id)) return prev;
      return [toast, ...prev].slice(0, MAX_TOASTS);
    });
  }, []);

  const dismiss = useCallback((id: number) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  useEffect(() => {
    if (!rejection) return;
    push({
      id: rejection.receivedAtMs,
      tone: "error",
      title: rejection.source === "local" ? "操作が届きませんでした" : "操作が拒否されました",
      lines: [rejection.reason, `command: ${rejection.command}`],
      expiresAt: Date.now() + REJECTION_TTL_MS,
    });
    clearRejection();
  }, [rejection, clearRejection, push]);

  useEffect(() => {
    const latest = healthEvents[0];
    if (!latest || latest.level === "info") return;
    push({
      id: latest.receivedAtMs,
      tone: latest.level === "critical" ? "error" : "warning",
      title: `${String(latest.level).toUpperCase()} — ${latest.robot}`,
      lines: [
        `${latest.target}: ${latest.from} → ${latest.to}`,
        ...(latest.message ? [latest.message] : []),
      ],
      expiresAt: Date.now() + HEALTH_TTL_MS,
    });
  }, [healthEvents, push]);

  useEffect(() => {
    if (toasts.length === 0) return;
    const timers = toasts.map((t) =>
      setTimeout(() => dismiss(t.id), Math.max(0, t.expiresAt - Date.now())),
    );
    return () => timers.forEach(clearTimeout);
  }, [toasts, dismiss]);

  if (toasts.length === 0) return null;

  // daisyUI の `.modal` は z-index: 999。`.toast` は `pointer-events: none` を持たず
  // 幅が `calc(100vw - 2rem)` なので、1000 へ上げるなら透過も自分で当てる。
  return (
    <div className="pointer-events-none toast toast-end toast-bottom z-[1000]">
      {toasts.map((toast) => (
        <ToastCard key={toast.id} toast={toast} onDismiss={() => dismiss(toast.id)} />
      ))}
    </div>
  );
}
