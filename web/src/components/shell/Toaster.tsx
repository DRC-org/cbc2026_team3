import { OctagonAlert, TriangleAlert, X } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { Icon } from "@/components/ui/Icon";
import { useRobotCommands, useRobotStatus } from "@/context/RobotContext";

const REJECTION_TTL_MS = 5000;
const HEALTH_TTL_MS = 6000;
// 同時表示を絞らないと古い通知が画面下部を埋め、直近の異常が読めなくなる
const MAX_TOASTS = 3;

type Tone = "warning" | "danger";

interface ToastItem {
  id: number;
  tone: Tone;
  title: string;
  lines: string[];
  expiresAt: number;
}

/** daisyUI の alert は「alert + 色修飾子」で 1 組。片方だけだと配色ルールごと消える */
const TONE_ALERT_CLASS: Record<Tone, string> = {
  warning: "alert alert-warning",
  danger: "alert alert-error",
};

const TONE_ICON = {
  warning: TriangleAlert,
  danger: OctagonAlert,
} as const;

function ToastCard({ toast, onDismiss }: { toast: ToastItem; onDismiss: () => void }) {
  return (
    <div
      role="alert"
      className={`${TONE_ALERT_CLASS[toast.tone]} w-[22rem] max-w-[calc(100vw-2rem)] items-start gap-2 p-2`}
    >
      <Icon as={TONE_ICON[toast.tone]} className="mt-[0.15em] text-[1.1em]" />
      <div className="min-w-0 flex-1">
        <div className="font-bold">{toast.title}</div>
        {toast.lines.map((line) => (
          <div key={line} className="truncate opacity-90">
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

/**
 * 全画面共通の通知スタック。
 *
 * 以前は「操作拒否」と「ヘルス異常」をそれぞれ別実装で出しており、
 * 同時発生時に重なって読めなくなっていた。
 * 表示位置と寿命の管理をここへ一本化し、常に右下から積み上げる。
 */
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

  // 拒否は受け取った時点でトーストへ移し替える。同じ操作を続けて拒否された場合にも
  // 再表示されるよう、コンテキスト側の状態はすぐに空へ戻す
  useEffect(() => {
    if (!rejection) return;
    push({
      id: rejection.receivedAtMs,
      tone: "danger",
      title: "操作が拒否されました",
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
      tone: latest.level === "critical" ? "danger" : "warning",
      title: `${latest.level.toUpperCase()} — ${latest.robot}`,
      lines: [
        `${latest.target}: ${latest.from} → ${latest.to}`,
        ...(latest.message ? [latest.message] : []),
      ],
      expiresAt: Date.now() + HEALTH_TTL_MS,
    });
  }, [healthEvents, push]);

  // 絶対時刻で管理し、リスト更新のたびに張り直しても寿命がずれないようにする
  useEffect(() => {
    if (toasts.length === 0) return;
    const timers = toasts.map((t) =>
      setTimeout(() => dismiss(t.id), Math.max(0, t.expiresAt - Date.now())),
    );
    return () => timers.forEach(clearTimeout);
  }, [toasts, dismiss]);

  if (toasts.length === 0) return null;

  // ステータスバーに被らないよう底を持ち上げる
  return (
    <div className="toast toast-end toast-bottom bottom-8 z-50">
      {toasts.map((toast) => (
        <ToastCard key={toast.id} toast={toast} onDismiss={() => dismiss(toast.id)} />
      ))}
    </div>
  );
}
