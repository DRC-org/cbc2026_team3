import type { ReactNode } from "react";
import { useCallback, useEffect, useRef, useState } from "react";

import { cx } from "@/lib/cx";

// オーバーレイ型スクロールバーの環境では `offsetWidth - clientWidth` が 0 になるので、
// 溢れの有無は scrollTop / clientHeight / scrollHeight から測る
export function ScrollArea({ className, children }: { className?: string; children: ReactNode }) {
  const ref = useRef<HTMLDivElement | null>(null);
  const [edges, setEdges] = useState({ above: false, below: false });

  const measure = useCallback(() => {
    const el = ref.current;
    if (!el) return;
    const above = el.scrollTop > 1;
    const below = el.scrollTop + el.clientHeight < el.scrollHeight - 1;
    setEdges((prev) => (prev.above === above && prev.below === below ? prev : { above, below }));
  }, []);
  useEffect(measure);
  useEffect(() => {
    window.addEventListener("resize", measure);
    return () => window.removeEventListener("resize", measure);
  }, [measure]);

  return (
    <div className="relative flex min-h-0 flex-1 flex-col">
      <div
        ref={ref}
        onScroll={measure}
        className={cx("scroll flex min-h-0 flex-1 flex-col", className)}
      >
        {children}
      </div>
      {edges.above ? (
        <div
          aria-hidden
          className="pointer-events-none absolute inset-x-0 top-0 h-4 bg-linear-to-b from-base-content/18 to-transparent"
        />
      ) : null}
      {edges.below ? (
        <div
          aria-hidden
          className="pointer-events-none absolute inset-x-0 bottom-0 h-4 bg-linear-to-t from-base-content/18 to-transparent"
        />
      ) : null}
    </div>
  );
}
