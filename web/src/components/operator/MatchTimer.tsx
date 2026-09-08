import { Panel } from "@/components/ui/Panel";
import { useRemainingMs } from "@/hooks/useRemainingMs";
import type { MatchTimer as MatchTimerValue } from "@/lib/protocol";
import { formatRemaining } from "@/lib/time";

/**
 * 操縦者画面の試合時間。**数字が「残り」であることを legend で言い切る。**
 *
 * 「試合時間」では残りなのか経過なのかが画面から読めない —— 視線を戻した一瞬で
 * 誤読すると、残り 0:30 を「30 秒しか経っていない」と読んで手順を選び直す。
 *
 * 残り時間の算術と、秒境界に合わせた起床は `hooks/useRemainingMs.ts` が持つ。
 * Monitor の `MatchStrip` も同じフックを使うので、ここに書き写してはならない。
 *
 * **右カラムでは縮まない側に置く (`shrink-0`)。** 高さは中身で決まり切っていて
 * 削れる余地が無いのに、flex の既定 (`flex-shrink: 1`) は隣の機体状態パネルが
 * 伸びたぶんをここからも取る —— caption が数字の下半分に重なって読めなくなる。
 * 縮むのは内部スクロールを持つ機体状態パネルの側でなければならない。
 * **クロス軸 (幅) の `self-start` を戻してはならない**（別の不具合になる）。
 */
interface MatchTimerProps {
  timer: MatchTimerValue | null;
}

export function MatchTimer({ timer }: MatchTimerProps) {
  const remaining = useRemainingMs(timer);

  if (remaining === null) {
    return (
      <Panel legend="残り時間" className="shrink-0">
        <div className="text-center text-[1.1em] text-base-content/60">タイマー未受信</div>
      </Panel>
    );
  }

  // 進行中の数字が残り時間であることは legend が言うので何も足さない。停止中の 2 つは
  // legend からは読めない別の事実なので必ず出す —— 同じ 0:30 でも「まだ始まっていない」
  // のか「その残りを抱えて終わった」のかで意味が正反対になる
  const caption = timer?.running ? null : timer?.elapsed_ms === 0 ? "開始前" : "試合終了時点";

  return (
    <Panel legend="残り時間" className="shrink-0">
      <div className="flex flex-col items-center gap-[0.1em] py-1">
        <span className="font-mono text-[3.4em] leading-none font-bold tabular-nums">
          {formatRemaining(remaining)}
        </span>
        {caption ? <span className="text-[0.8em] text-base-content/60">{caption}</span> : null}
      </div>
    </Panel>
  );
}
