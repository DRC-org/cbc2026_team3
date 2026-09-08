import { SlidersHorizontal, Workflow } from "lucide-react";

import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { cx } from "@/lib/cx";
import type { OperationMode } from "@/lib/protocol";
import { TONE_BORDER_L_CLASS } from "@/lib/tone";

interface ModeSwitchProps {
  mode: OperationMode;
  onChange: (mode: OperationMode) => void;
  /** 切り替えられない理由。null なら切り替えられる */
  blockedReason: string | null;
  /** 実行対象のシーケンス名 */
  sequenceName: string;
  /**
   * 総ステップ数。null なら出さない。
   * 試合中は `ActionPanel` が `1/22` の形で総数を出しているので、そちらでは null を渡す
   * （同じ事実を 2 度描かない）。
   */
  totalSteps: number | null;
}

/**
 * 操作モードの切り替え。**ページ最上段に独立した帯として置く** —— パネルの見出し行へ
 * 埋めると、機体を直接動かせる状態に入っていることが視線を戻した一瞬では読めなくなる。
 *
 * **タブの形を使わない**（docs/invariants.md 「モード帯とヘッダーのタブ帯は見た目を
 * 分ける」）。手動中は帯そのものを警告色にするが、**高さはモードで変えない** ——
 * 変えると下の主操作 (`ActionPanel`) が上下にずれ、押す直前に探し直すことになる。
 *
 * **切替ボタンは帯の先頭に置く**（docs/invariants.md 「EMG STOP の周囲に押下可能な
 * 要素を置かない」）。この帯はヘッダーへ密着しているので、右端に置いたボタンは
 * EMG STOP の真下数 px に来る。**右端に残してよいのは押せない要素だけ。** 先頭に
 * 固定するのは、現在モードのチップの文言長がモードで変わり、その隣だとボタンの横位置が
 * 動くため。
 *
 * 手動操縦のアイコンに `Hand` を使わない（あちらは「許可待ち / ここで停止」専用）。
 */
export function ModeSwitch({
  mode,
  onChange,
  blockedReason,
  sequenceName,
  totalSteps,
}: ModeSwitchProps) {
  const manual = mode === "manual";
  const next: OperationMode = manual ? "sequence" : "manual";

  return (
    // ページ余白を打ち消してヘッダー直下へ密着させる。帯が浮いていると
    // 「この画面全体が今どのモードか」ではなく 1 つの部品の状態に見える
    <div
      className={cx(
        "-mx-2 -mt-2 flex shrink-0 items-center gap-2 border-b border-l-[0.4rem] border-base-300 px-2 py-[0.15rem]",
        TONE_BORDER_L_CLASS[manual ? "warning" : "neutral"],
        manual ? "bg-warning/10" : "bg-base-100",
      )}
    >
      {/* 帯の先頭が切替ボタンの定位置。EMG STOP の真下 (右端) には押せる要素を置かず、
          モードでも塞がれ状態でも位置が動かないこの位置に固定する */}
      <Button
        tone={manual ? "default" : "warn"}
        className="h-[1.5rem] min-h-0 shrink-0 px-2"
        disabled={blockedReason !== null}
        onClick={() => onChange(next)}
      >
        <Icon as={manual ? Workflow : SlidersHorizontal} />
        {manual ? "半自動へ戻る" : "手動操縦へ"}
      </Button>

      <StatusBadge tone={manual ? "warning" : "neutral"} className="shrink-0">
        <span className="flex items-center gap-1.5">
          <Icon as={manual ? SlidersHorizontal : Workflow} />
          {manual ? "手動操縦中 — シーケンスは停止しています" : "半自動"}
        </span>
      </StatusBadge>

      <span className="min-w-0 truncate font-mono text-base-content/70">{sequenceName}</span>
      {totalSteps === null ? null : (
        <span className="shrink-0 text-base-content/70">全 {totalSteps} ステップ</span>
      )}

      {/* 塞がれている理由だけが右端へ寄る。押せない要素なので EMG STOP の真下でよい */}
      {blockedReason ? (
        <span className="ml-auto min-w-0 truncate text-base-content/70">{blockedReason}</span>
      ) : null}
    </div>
  );
}
