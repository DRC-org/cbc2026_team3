import { ArrowRight, Ban, Hand, Play, Square, TriangleAlert } from "lucide-react";

import { TriggerButton } from "@/components/operator/TriggerButton";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { Panel } from "@/components/ui/Panel";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { cx } from "@/lib/cx";
import type { RobotState } from "@/lib/protocol";
import {
  isRestartFromTop,
  isSequenceComplete,
  sequenceKind,
  sequenceProgress,
} from "@/lib/sequenceStatus";
import type { Tone } from "@/lib/tone";
import { TONE_PROGRESS_CLASS } from "@/lib/tone";

interface ActionPanelProps {
  state: RobotState;
  inMatch: boolean;
  blockedLabel: string;
  blocked: boolean;
  onStart: () => void;
  onStop: () => void;
  onTrigger: () => void;
}

const PRIMARY_CLASS = "h-full w-full rounded-none border-0 text-[1.3em]";

export function ActionPanel({
  state,
  inMatch,
  blockedLabel,
  blocked,
  onStart,
  onStop,
  onTrigger,
}: ActionPanelProps) {
  const { total_steps: totalSteps, step_index: stepIndex } = state;
  const steps = state.steps ?? [];

  const kind = sequenceKind(state);
  const restartFromTop = isRestartFromTop(state);
  const isComplete = isSequenceComplete(state);
  const canStop = kind === "running" || kind === "waiting_trigger";
  const { displayIndex, percent, current } = sequenceProgress(state);
  const burst: typeof steps = [];
  if (!isComplete) {
    for (let i = stepIndex + 1; i < steps.length; i += 1) {
      burst.push(steps[i]);
      if (steps[i].require_trigger) break;
    }
  }
  const burstEnd = burst.at(-1);
  const stopsAtTrigger = burstEnd?.require_trigger ?? false;
  const burstMessage = isComplete
    ? "終了"
    : burstEnd === undefined
      ? "最終ステップ"
      : stopsAtTrigger
        ? `${burst.length} ステップ先「${burstEnd.label}」で停止`
        : `残り ${burst.length} ステップ 停止なし`;
  const idle = inMatch && kind === "idle";

  const status: { label: string; tone: Tone } = !inMatch
    ? { label: blockedLabel, tone: "neutral" }
    : kind === "no_sequence"
      ? { label: "シーケンス未取得", tone: "neutral" }
      : kind === "complete"
        ? { label: "完走", tone: "success" }
        : kind === "waiting_trigger"
          ? { label: "許可待ち", tone: "warning" }
          : kind === "running"
            ? { label: "実行中", tone: "info" }
            : restartFromTop
              ? { label: "停止中", tone: "warning" }
              : { label: "待機中", tone: "neutral" };

  return (
    <Panel accentTone={status.tone} className="shrink-0" bodyClassName="p-0">
      <div className="flex shrink-0 items-center gap-2 border-b border-base-300 px-2 py-1">
        <StatusBadge tone={status.tone}>{status.label}</StatusBadge>
      </div>
      <progress
        className={cx(
          "progress h-[0.35rem] w-full shrink-0 rounded-none bg-base-200",
          TONE_PROGRESS_CLASS[status.tone],
        )}
        value={percent}
        max={100}
      />

      {state.last_error ? (
        <div className="flex shrink-0 items-start gap-1.5 border-l-[0.25rem] border-l-error bg-error/5 px-3 py-1">
          <Icon as={TriangleAlert} className="mt-[0.2em] shrink-0 text-error" />
          <span className="min-w-0">
            <span className="mr-2 font-medium">
              ステップ {state.last_error.step_index + 1}「{state.last_error.step}」で停止
            </span>
            <span className="text-base-content/80">{state.last_error.message}</span>
          </span>
        </div>
      ) : null}

      <div className="flex min-h-[5.5rem] shrink-0 items-center px-4 py-3">
        <div className="flex min-w-0 items-baseline gap-4">
          <span className="shrink-0 font-mono text-[3em] leading-none text-base-content/30 tabular-nums">
            {displayIndex}
            <span className="text-[0.45em] text-base-content/40">/{totalSteps}</span>
          </span>
          {isComplete ? null : (
            <span className="min-w-0 text-[3em] leading-[1.1] font-semibold">
              {current?.label ?? "—"}
            </span>
          )}
        </div>
      </div>

      <div className="flex shrink-0 items-center gap-2 border-t border-base-300 px-4 py-2">
        <Icon
          as={stopsAtTrigger ? Hand : ArrowRight}
          className={stopsAtTrigger ? "text-warning" : "text-base-content/60"}
        />
        <span className="min-w-0 truncate text-base-content/80">{burstMessage}</span>
      </div>

      <div className="grid min-h-[5.5rem] shrink-0 grid-cols-[minmax(9rem,0.28fr)_1fr] gap-px border-t border-base-300 bg-base-300">
        <Button
          tone="danger"
          disabled={!inMatch || !canStop || blocked}
          onClick={onStop}
          aria-label="シーケンスを通常停止"
          className={PRIMARY_CLASS}
        >
          <Icon as={Square} />
          STOP
        </Button>

        {idle ? (
          <Button
            tone={restartFromTop ? "warn" : "ok"}
            disabled={blocked}
            onClick={onStart}
            aria-label={restartFromTop ? "シーケンスを先頭から再開" : "シーケンスを先頭から開始"}
            className={PRIMARY_CLASS}
          >
            <Icon as={blocked ? Ban : Play} />
            {restartFromTop ? "先頭から再開" : "START"}
          </Button>
        ) : (
          <TriggerButton
            kind={kind}
            onTrigger={onTrigger}
            disabled={!inMatch || blocked}
            disabledLabel={inMatch ? null : blockedLabel}
          />
        )}
      </div>
    </Panel>
  );
}
