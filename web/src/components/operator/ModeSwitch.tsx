import type { LucideIcon } from "lucide-react";
import { SlidersHorizontal, TriangleAlert, Workflow } from "lucide-react";

import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
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

const MODE_OPTIONS: {
  value: OperationMode;
  label: string;
  icon: LucideIcon;
  selectedClass: string;
}[] = [
  {
    value: "sequence",
    label: "半自動",
    icon: Workflow,
    selectedClass: "border-neutral bg-neutral text-neutral-content",
  },
  {
    value: "manual",
    label: "手動操縦",
    icon: SlidersHorizontal,
    selectedClass: "border-warning bg-warning text-warning-content",
  },
];

export function ModeSwitch({
  mode,
  onChange,
  blockedReason,
  sequenceName,
  totalSteps,
}: ModeSwitchProps) {
  const manual = mode === "manual";

  return (
    <div
      className={cx(
        "-mx-2 -mt-2 flex shrink-0 items-center gap-2 border-b border-l-[0.4rem] border-base-300 px-2 py-[0.15rem]",
        TONE_BORDER_L_CLASS[manual ? "warning" : "neutral"],
        manual ? "bg-warning/10" : "bg-base-100",
      )}
    >
      <div className="join shrink-0" role="group" aria-label="操作モード">
        {MODE_OPTIONS.map((opt) => {
          const selected = opt.value === mode;
          return (
            <Button
              key={opt.value}
              className={cx("join-item h-[1.5rem] min-h-0 px-2", selected && opt.selectedClass)}
              disabled={blockedReason !== null}
              aria-pressed={selected}
              onClick={() => {
                if (!selected) onChange(opt.value);
              }}
            >
              <Icon as={opt.icon} />
              {opt.label}
            </Button>
          );
        })}
      </div>

      {manual ? (
        <span className="flex shrink-0 items-center gap-1.5 text-warning">
          <Icon as={TriangleAlert} />
          シーケンス停止中
        </span>
      ) : null}

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
