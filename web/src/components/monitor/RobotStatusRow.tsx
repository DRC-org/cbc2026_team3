import { Hand } from "lucide-react";

import { SubsystemStatus } from "@/components/diagnostics/SubsystemStatus";
import { Icon } from "@/components/ui/Icon";
import { Panel } from "@/components/ui/Panel";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { cx } from "@/lib/cx";
import type { TempThresholds } from "@/lib/healthVerdict";
import type { RobotState } from "@/lib/protocol";
import { isSequenceComplete, sequenceKind, sequenceProgress } from "@/lib/sequenceStatus";
import type { SequenceKind } from "@/lib/sequenceStatus";
import type { Tone } from "@/lib/tone";
import { TONE_PROGRESS_CLASS } from "@/lib/tone";

interface RobotStatusRowProps {
  label: string;
  state: RobotState | undefined;
  connected: boolean;
  tempThresholds?: TempThresholds | null;
}

const ACTIVITY: Record<SequenceKind, { tone: Tone; label: string }> = {
  no_sequence: { tone: "neutral", label: "シーケンス未取得" },
  idle: { tone: "neutral", label: "待機中" },
  waiting_trigger: { tone: "warning", label: "許可待ち" },
  running: { tone: "info", label: "実行中" },
  complete: { tone: "success", label: "完走" },
};

export function RobotStatusRow({
  label,
  state,
  connected,
  tempThresholds = null,
}: RobotStatusRowProps) {
  if (!state) {
    return (
      <div className="card flex shrink-0 items-center gap-3 border-base-300 bg-base-100 p-2 card-border">
        <span className="text-[1.2em] font-semibold">{label}</span>
        <StatusBadge tone="error">データ未受信</StatusBadge>
      </div>
    );
  }

  const inManual = state.manual?.mode === "manual";
  const activity = ACTIVITY[sequenceKind(state)];
  const { waiting_trigger: waiting } = state;
  const isComplete = isSequenceComplete(state);
  const { displayIndex, total, percent, current } = sequenceProgress(state);

  return (
    <Panel accentTone={activity.tone} bodyClassName="p-0">
      <div className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-1 px-2 py-1.5">
        <span className="text-[1.2em] font-semibold">{label}</span>
        <StatusBadge tone={activity.tone}>{activity.label}</StatusBadge>
        {inManual ? <StatusBadge tone="warning">手動操縦中</StatusBadge> : null}
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

      <div className="flex min-w-0 shrink-0 items-center gap-2 px-2 py-1.5 text-[1.15em]">
        {waiting ? <Icon as={Hand} className="shrink-0 text-warning" /> : null}
        <span className="min-w-0 truncate">
          {isComplete ? "全ステップ完了" : (current?.label ?? "—")}
        </span>
      </div>

      <div className="flex min-h-0 flex-1 flex-col border-t border-base-300 px-1 py-1">
        <SubsystemStatus
          health={state.health}
          motors={state.motors}
          safety={state.safety}
          sensors={state.sensors}
          connected={connected}
          tempThresholds={tempThresholds}
          defaultOpen
        />
      </div>
    </Panel>
  );
}
