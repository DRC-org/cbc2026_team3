import { useRobot } from "@/context/RobotContext";
import type { HealthChangeLevel } from "@/hooks/useRobotSocket";
import { cx } from "@/lib/cx";
import type { Tone } from "@/lib/tone";
import { TONE_STATUS_CLASS } from "@/lib/tone";

const LEVEL_TONE: Record<HealthChangeLevel, Tone> = {
  info: "neutral",
  warning: "warning",
  critical: "error",
};

function formatTime(ms: number): string {
  return new Date(ms).toLocaleTimeString("ja-JP", { hour12: false });
}

/**
 * ヘルス変化の履歴。
 *
 * これまでヘルス異常は数秒で消えるトーストにしか出ていなかった。Monitor の
 * 担当は「試合中に起きたことを拾って後で共有する」役割なのに、目を離した
 * 数秒の間に起きた事象は痕跡ごと消えていた。試合中の画面に残す。
 */
export function EventFeed() {
  const { healthEvents } = useRobot();

  if (healthEvents.length === 0) {
    return (
      <p className="px-1 py-2 text-base-content/60">
        異常イベントはありません。ここに CAN・モータの状態変化が新しい順に残ります。
      </p>
    );
  }

  return (
    <ol className="scroll min-h-0 flex-1 [&>*:nth-child(odd)]:bg-base-200">
      {healthEvents.map((ev) => {
        const tone = LEVEL_TONE[ev.level];
        return (
          <li
            key={`${ev.receivedAt}-${ev.target}`}
            className="flex min-w-0 items-baseline gap-2 px-1 py-[0.15rem]"
          >
            <span
              className={cx(TONE_STATUS_CLASS[tone], "translate-y-[-0.1em] shrink-0")}
              aria-hidden
            />
            <span className="shrink-0 font-mono text-[0.85em] text-base-content/60 tabular-nums">
              {formatTime(ev.receivedAt)}
            </span>
            <span className="shrink-0 text-base-content/70">{ev.robot}</span>
            <span className="min-w-0 flex-1 truncate">
              {ev.target}: {ev.from} → {ev.to}
              {ev.message ? ` (${ev.message})` : ""}
            </span>
          </li>
        );
      })}
    </ol>
  );
}
