import { Panel } from "@/components/ui/Panel";
import { useRemainingMs } from "@/hooks/useRemainingMs";
import type { MatchTimer as MatchTimerValue } from "@/lib/protocol";
import { formatRemaining } from "@/lib/time";

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
