import { Hand, Workflow } from "lucide-react";

import { Icon } from "@/components/ui/Icon";
import { Panel } from "@/components/ui/Panel";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { cx } from "@/lib/cx";
import type { OperationMode } from "@/lib/protocol";

interface ModeSwitchProps {
  mode: OperationMode;
  onChange: (mode: OperationMode) => void;
  /** 切り替えられない理由。null なら切り替えられる */
  blockedReason: string | null;
}

const MODES: { value: OperationMode; label: string; icon: typeof Hand }[] = [
  { value: "sequence", label: "半自動", icon: Workflow },
  { value: "manual", label: "手動操縦", icon: Hand },
];

/**
 * 操作モードの切り替え。**ページ最上段に独立した帯として置く。**
 *
 * 主操作 (`ActionPanel`) の上へ積むのは、その位置を状態で動かさない約束と
 * 両立する — モードが変われば下の面ごと差し替わるので、主操作を探し直すのは
 * どのみち 1 回だけ。逆にパネルの見出し行へ埋めると、機体を直接動かせる状態に
 * 入っていることが視線を戻した一瞬では読めなくなる。
 *
 * 手動中は帯そのものを警告色で示す。「今この画面から機体を直接動かせる」ことは
 * 平常時と最も強く区別されるべき事実で、Monitor 側にも同じチップが出る。
 */
export function ModeSwitch({ mode, onChange, blockedReason }: ModeSwitchProps) {
  const manual = mode === "manual";

  return (
    // 手動中は帯そのものを警告色にする。色は TONE_BORDER_L_CLASS が唯一の出どころ
    <Panel
      accentTone={manual ? "warning" : "neutral"}
      className="shrink-0"
      bodyClassName="flex-row items-center gap-3 p-0 px-2 py-1"
    >
      {/* daisyUI の tabs は「親クラス + 状態クラス」が揃って初めて成立する。
          tab-active を分離して組み立てると選択中の見た目ごと消える */}
      <div role="tablist" className="tabs tabs-box shrink-0 bg-base-200 tabs-sm">
        {MODES.map(({ value, label, icon }) => (
          <button
            key={value}
            type="button"
            role="tab"
            aria-selected={mode === value}
            aria-label={`${label}へ切り替え`}
            disabled={blockedReason !== null && mode !== value}
            onClick={() => onChange(value)}
            className={cx("tab gap-1.5", mode === value && "tab-active")}
          >
            <Icon as={icon} />
            {label}
          </button>
        ))}
      </div>

      {manual ? (
        <StatusBadge tone="warning">手動操縦中 — シーケンスは停止しています</StatusBadge>
      ) : (
        <span className="text-base-content/60">シーケンス制御中</span>
      )}

      {blockedReason ? (
        <span className="ml-auto min-w-0 truncate text-base-content/70">{blockedReason}</span>
      ) : null}
    </Panel>
  );
}
