import { Hand } from "lucide-react";

import { SubsystemStatus } from "@/components/diagnostics/SubsystemStatus";
import { Icon } from "@/components/ui/Icon";
import { StatusBadge } from "@/components/ui/StatusBadge";
import type { RobotState } from "@/hooks/useRobotSocket";
import { cx } from "@/lib/cx";
import type { Tone } from "@/lib/tone";
import { TONE_BORDER_L_CLASS, TONE_PROGRESS_CLASS } from "@/lib/tone";

interface RobotStatusRowProps {
  label: string;
  state: RobotState | undefined;
}

interface Activity {
  tone: Tone;
  label: string;
}

/**
 * 試合中の Monitor が最初に読むべき「その機体は今どうなっているか」。
 * 進行中か、操縦者の操作待ちで止まっているか、完走したか。
 */
function activityOf(state: RobotState): Activity {
  const { total_steps: total, step_index: index, waiting_trigger: waiting } = state;
  if (total === 0) return { tone: "neutral", label: "シーケンス未取得" };
  if (index >= total && !waiting) return { tone: "success", label: "完走" };
  if (waiting) return { tone: "warning", label: "許可待ち" };
  if (index === 0) return { tone: "neutral", label: "待機中" };
  return { tone: "info", label: "実行中" };
}

/**
 * Monitor 試合中の 1 機分。
 *
 * 以前はここに 8 モータ × 4 値の表が常時展開されていた。両機ぶんで 64 個の数字が
 * 画面を埋め、肝心の「どちらの機体が止まっていて誰の操作待ちか」が沈んでいた。
 * 進行状態を主役に据え、数値は SubsystemStatus の判定 1 行へ畳んでいる。
 */
export function RobotStatusRow({ label, state }: RobotStatusRowProps) {
  if (!state) {
    return (
      <div className="card flex shrink-0 items-center gap-3 border-base-300 bg-base-100 p-2 card-border">
        <span className="text-[1.2em] font-semibold">{label}</span>
        <StatusBadge tone="error">データ未受信</StatusBadge>
      </div>
    );
  }

  const activity = activityOf(state);
  const { total_steps: total, step_index: index, waiting_trigger: waiting } = state;
  const isComplete = total > 0 && index >= total && !waiting;
  const displayIndex = total > 0 ? Math.min(index + 1, total) : 0;
  const percent = total > 0 ? Math.min(100, ((isComplete ? total : index + 1) / total) * 100) : 0;
  const steps = state.steps ?? [];
  const current = isComplete ? null : steps[index];

  return (
    <section
      className={cx(
        "card card-border flex min-h-0 flex-col border-base-300 border-l-[0.3rem] bg-base-100",
        TONE_BORDER_L_CLASS[activity.tone],
      )}
    >
      <div className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-1 px-2 py-1.5">
        <span className="text-[1.2em] font-semibold">{label}</span>
        <StatusBadge tone={activity.tone}>{activity.label}</StatusBadge>
        <span className="ml-auto shrink-0 font-mono text-base-content/70 tabular-nums">
          {displayIndex}
          <span className="text-base-content/45">/{total}</span>
        </span>
      </div>

      <progress
        className={cx(
          "progress h-[0.3rem] w-full shrink-0 rounded-none bg-base-200",
          TONE_PROGRESS_CLASS[activity.tone],
        )}
        value={percent}
        max={100}
      />

      {/* 現在ステップ。Monitor は操作しないので、読めれば十分な大きさに留める */}
      <div className="flex min-w-0 shrink-0 items-center gap-2 px-2 py-1.5 text-[1.15em]">
        {waiting ? <Icon as={Hand} className="shrink-0 text-warning" /> : null}
        <span className="min-w-0 truncate">
          {isComplete ? "全ステップ完了" : (current?.label ?? "—")}
        </span>
      </div>

      {/* Monitor は操縦しない役で、数値を追う時間がある。操縦者側の同じ部品は
          畳んだままにしてあり、既定の開閉だけを役割で変えている */}
      <div className="flex min-h-0 flex-1 flex-col border-t border-base-300 px-1 py-1">
        <SubsystemStatus health={state.health} motors={state.motors} defaultOpen />
      </div>
    </section>
  );
}
