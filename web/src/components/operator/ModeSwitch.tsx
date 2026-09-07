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
 * 操作モードの切り替え。**ページ最上段に独立した帯として置く。**
 *
 * パネルの見出し行へ埋めると、機体を直接動かせる状態に入っていることが視線を
 * 戻した一瞬では読めなくなる。一方で枠付きパネルにもしない —— 罫線 1 本と左端の
 * アクセントバーで足りる事実に、枠と本文余白ぶんの縦を払う理由が無い。
 *
 * **タブの形を使わない。** ヘッダーの画面切替タブと同じ見た目が上下に 2 段並ぶと、
 * 「見る場所を変える操作」と「機体の制御権を奪う操作」が同じ形になる。
 * 現在モードは状態チップで示し、切替は行き先を書いたボタン 1 つに絞る。
 *
 * 手動中は帯そのものを警告色にする。「今この画面から機体を直接動かせる」ことは
 * 平常時と最も強く区別されるべき事実で、Monitor 側にも同じチップが出る。
 * **高さはモードで変えない** —— 変えると下の主操作 (`ActionPanel`) が上下にずれ、
 * 押す直前に探し直すことになる。
 *
 * 手動操縦のアイコンに `Hand` を使わない。あちらは「許可待ち / ここで停止」の意味で
 * `ActionPanel` / `SequenceStepList` / `RobotStatusRow` が使っており、試合中に同じ手が
 * 2 つの意味で出ると、トリガー待ちなのか手動操縦なのか区別できない。
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

      {/* 塞がれている理由はボタンの手前に置く。右端はボタンの定位置で、
          理由の有無で切替ボタンが横に動くと押す直前に探し直しになる */}
      <div className="ml-auto flex min-w-0 items-center gap-2">
        {blockedReason ? (
          <span className="min-w-0 truncate text-base-content/70">{blockedReason}</span>
        ) : null}
        <Button
          tone={manual ? "default" : "warn"}
          className="h-[1.5rem] min-h-0 shrink-0 px-2"
          disabled={blockedReason !== null}
          onClick={() => onChange(next)}
        >
          <Icon as={manual ? Workflow : SlidersHorizontal} />
          {manual ? "半自動へ戻る" : "手動操縦へ"}
        </Button>
      </div>
    </div>
  );
}
