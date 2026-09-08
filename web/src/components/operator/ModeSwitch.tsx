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
  blockedReason: string | null;
  sequenceName: string;
  totalSteps: number | null;
}

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
    <div
      className={cx(
        "-mx-2 -mt-2 flex shrink-0 items-center gap-2 border-b border-l-[0.4rem] border-base-300 px-2 py-[0.15rem]",
        TONE_BORDER_L_CLASS[manual ? "warning" : "neutral"],
        manual ? "bg-warning/10" : "bg-base-100",
      )}
    >
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

      {blockedReason ? (
        <span className="ml-auto min-w-0 truncate text-base-content/70">{blockedReason}</span>
      ) : null}
    </div>
  );
}
