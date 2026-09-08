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
 * **切替ボタンは帯の先頭に置く。EMG STOP の真下に押下可能な要素を置かないため。**
 * この帯はページ余白を打ち消してヘッダーへ密着しており、右端に置いたボタンは
 * ヘッダー右端の EMG STOP の真下数 px に来る。誤爆の向きは「切替ボタンを押そうとして
 * EMG STOP を踏む」で、試合中に起きるとシーケンスが止まる。全画面のうち EMG STOP へ
 * これほど近い押下可能要素はここだけだった。**右端に残してよいのは押せない要素
 * (塞がれている理由) だけ。**
 *
 * **先頭に固定するのは、位置がモードでも塞がれ状態でも動かないため。** 現在モードの
 * チップは文言長がモードで変わる (「手動操縦中 — シーケンスは停止しています」と
 * 「半自動」) ので、その隣に置くとボタンの横位置がモードで動き、「主操作は状態に
 * よって位置を動かさない」が帯の側から破られる。
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
