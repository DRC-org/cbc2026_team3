import { memo } from "react";

import { StatusBadge } from "@/components/ui/StatusBadge";
import type { AdviceSeverity, TuningAdvice } from "@/lib/protocol";
import type { Tone } from "@/lib/tone";

/**
 * 指標から導いた助言。
 *
 * **判定はサーバーが持つ。** UI は届いた文言を並べるだけで、指標から助言を
 * 導き直さない。両方が判定を持つと「波形と指標は同じなのに助言だけ食い違う」
 * 状態が作れてしまい、操縦者はどちらを信じればよいか分からなくなる
 * (`lib/healthVerdict.ts` を 1 箇所に置いているのと同じ理由)。
 *
 * memo なのは `ResponseChart` と同じ理由。記録が増えていないのに、テレメトリで
 * 毎秒 40 回描き直されていた。
 *
 * 並び順もサーバーが決めた順のまま出す。飽和を先頭に置くことに意味があり
 * (飽和中はゲインを変えても応答が変わらない)、ここで並べ替えるとその順序が消える。
 */
export const AdviceList = memo(function AdviceList({ advice }: { advice: TuningAdvice[] }) {
  if (advice.length === 0) {
    return (
      <p className="text-base-content/60">
        ステップとして解釈できなかったため、指標と助言はありません。
      </p>
    );
  }

  return (
    <ul className="flex flex-col gap-2">
      {advice.map((item) => (
        <li key={item.code} className="flex items-start gap-2">
          <StatusBadge tone={TONE_OF[item.severity]} className="mt-[0.15em] shrink-0">
            {LABEL_OF[item.severity]}
          </StatusBadge>
          <span className="min-w-0 leading-snug">{item.message}</span>
        </li>
      ))}
    </ul>
  );
});

const TONE_OF: Record<AdviceSeverity, Tone> = {
  ok: "success",
  info: "info",
  warning: "warning",
};

const LABEL_OF: Record<AdviceSeverity, string> = {
  ok: "良好",
  info: "検討",
  warning: "注意",
};
