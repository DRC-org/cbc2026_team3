import { OctagonAlert, TriangleAlert, X } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { Icon } from "@/components/ui/Icon";
import { useRobotCommands, useRobotStatus } from "@/context/RobotContext";
import { cx } from "@/lib/cx";
import type { Tone } from "@/lib/tone";
import { TONE_ALERT_CLASS } from "@/lib/tone";

const REJECTION_TTL_MS = 5000;
const HEALTH_TTL_MS = 6000;
// 同時表示を絞らないと古い通知が画面下部を埋め、直近の異常が読めなくなる
const MAX_TOASTS = 3;

/**
 * トーストに出るのは「要確認」と「異常」だけ。成功や情報を積むと、直近の異常が
 * 古い通知に押し出される (同時表示は MAX_TOASTS で絞ってある)。
 *
 * **`lib/tone.ts` の `Tone` をローカル定義で覆い隠さない。** トーン名と配色表を
 * ここで自前に持つと、daisyUI の対を守る検査 (`lib/daisyPairs.test.tsx`) の対象から
 * 外れる。
 */
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
        // コンテナが `pointer-events-none` でクリックを透かすので、閉じるボタンの
        // ぶんだけここで受け直す (カード自体は下の操作を塞ぐが、面積は 22rem に
        // 留まるので、モーダルのフッターごと覆うことはない)
        "pointer-events-auto w-[22rem] max-w-[calc(100vw-2rem)] items-start gap-2 p-2",
      )}
    >
      <Icon as={TOAST_ICON[toast.tone]} className="mt-[0.15em] text-[1.1em]" />
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
 * 「操作拒否」と「ヘルス異常」を別々に出すと同時発生時に重なって読めなくなるので、
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
      tone: "error",
      // サーバーが断ったのか、そもそも届いていないのかで操縦者の次の一手が変わる
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
      // **`latest.level` を無検査で `.toUpperCase()` しない。** ここは
      // `RouteErrorBoundary`（`<Outlet />` だけを包む）の外にあるので、
      // 想定外の型 (受信境界の検査漏れ・将来の型変更) が来ても投げてはならない
      // ―― 投げれば緊急停止オーバーレイごと React ツリーがアンマウントする。
      // `String()` は何を渡しても例外にならない
      title: `${String(latest.level).toUpperCase()} — ${latest.robot}`,
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

  // **z は daisyUI の `.modal` (z-index: 999) より上に置く。** トーストと
  // 緊急停止オーバーレイは `AppShell` の兄弟で同じスタッキングコンテキストに居るので、
  // `z-50` のままだと**モーダル表示中のトーストが 62% の暗幕の下に沈む**。
  // そこが問題になるのは、まさに操縦者が説明を必要とする瞬間 ——
  // 切断中に緊急停止オーバーレイの Reset を押したときの
  // 「切断中のため送信できませんでした。機体側のラッチは残っています」は
  // `RootLayout` がトーストへ逃がしており、**それが唯一の説明経路**である。
  // 沈むと「Reset を押しても何も起きない」としか見えない。
  //
  // **上へ出した代わりに、コンテナはクリックを透かす。** daisyUI の `.toast` は
  // `pointer-events: none` を持たず `max-width: calc(100vw - 2rem)` なので、
  // モーダルより下に居たあいだは**構造的に**モーダルのボタンを塞げなかった。
  // 1000 へ上げるとその保護が外れ、ウィンドウ幅が約 1100px を下回るとトーストが
  // モーダルのフッターボタンに重なって押せなくなる (会場のノート PC で起きうる)。
  // 透かすのはコンテナだけで、閉じるボタンを活かすため `ToastCard` は受け直す。
  return (
    <div className="pointer-events-none toast toast-end toast-bottom z-[1000]">
      {toasts.map((toast) => (
        <ToastCard key={toast.id} toast={toast} onDismiss={() => dismiss(toast.id)} />
      ))}
    </div>
  );
}
