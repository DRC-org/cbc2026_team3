import { useState } from "react";

import { ManualAxisRow } from "@/components/operator/ManualAxisRow";
import { Kbd } from "@/components/ui/Kbd";
import { Panel } from "@/components/ui/Panel";
import { StatusBadge } from "@/components/ui/StatusBadge";
import type { RobotCommands } from "@/context/RobotContext";
import { useHotkeys } from "@/hooks/useHotkeys";
import type { ManualState } from "@/lib/protocol";

interface ManualPanelProps {
  robotKey: string;
  manual: ManualState;
  blockedReason: string | null;
  sendOrReport: RobotCommands["sendOrReport"];
}

const KEY_LEGEND: { keys: string[]; label: string }[] = [
  { keys: ["↑", "↓"], label: "軸" },
  { keys: ["←", "→"], label: "ジョグ" },
  { keys: ["[", "]"], label: "量" },
  { keys: ["Shift", "Home", "End"], label: "端" },
];

export function ManualPanel({ robotKey, manual, blockedReason, sendOrReport }: ManualPanelProps) {
  const [picked, setPicked] = useState<string | null>(null);

  const onJog = (axis: string, delta: number) =>
    sendOrReport({ type: "manual_jog", robot: robotKey, axis, delta }, "ジョグ");
  const onSet = (axis: string, value: number) =>
    sendOrReport({ type: "manual_set", robot: robotKey, axis, value }, "目標値の送信");
  const onMove = (axis: string, position: string) =>
    sendOrReport({ type: "manual_move", robot: robotKey, axis, position }, "プリセット移動");

  const steerable = manual.axes.filter((axis) => axis.manual !== null);
  const selected = steerable.some((axis) => axis.name === picked)
    ? picked
    : (steerable[0]?.name ?? null);

  const moveSelection = (direction: 1 | -1) => {
    const index = steerable.findIndex((axis) => axis.name === selected);
    if (index < 0) return;
    const next = Math.min(steerable.length - 1, Math.max(0, index + direction));
    setPicked(steerable[next].name);
  };

  useHotkeys(
    {
      ArrowUp: () => moveSelection(-1),
      ArrowDown: () => moveSelection(1),
    },
    steerable.length > 1,
  );

  return (
    <Panel
      legend="手動操縦"
      className="min-h-0 flex-1"
      bodyClassName="p-0"
      actions={blockedReason ? <StatusBadge tone="error">{blockedReason}</StatusBadge> : null}
    >
      {manual.axes.length === 0 ? (
        <p className="p-2 text-base-content/70">
          このロボットには手動操縦できる軸がありません (位置定数が未読込です)。
        </p>
      ) : (
        <>
          <div className="scroll flex min-h-0 flex-1 flex-col">
            {manual.axes.map((axis) => (
              <ManualAxisRow
                key={axis.name}
                axis={axis}
                blockedReason={blockedReason}
                selected={axis.name === selected}
                onSelect={() => {
                  if (axis.manual !== null) setPicked(axis.name);
                }}
                onJog={onJog}
                onSet={onSet}
                onMove={onMove}
              />
            ))}
          </div>

          {selected === null ? null : (
            <div className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-1 border-t border-base-300 px-2 py-1 text-[0.8em] text-base-content/55">
              {KEY_LEGEND.map(({ keys, label }) => (
                <span key={label} className="flex shrink-0 items-center gap-1">
                  {keys.map((key) => (
                    <Kbd key={key}>{key}</Kbd>
                  ))}
                  {label}
                </span>
              ))}
              <span className="ml-auto shrink-0">可動範囲内でのみ動きます</span>
            </div>
          )}
        </>
      )}
    </Panel>
  );
}
