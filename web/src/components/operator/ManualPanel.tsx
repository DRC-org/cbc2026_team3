import { useState } from "react";

import { ManualAxisRow } from "@/components/operator/ManualAxisRow";
import { OnOffPadGroup, splitOnOffAxes } from "@/components/operator/OnOffPadGroup";
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
  excludeAxes?: readonly string[];
}

const KEY_LEGEND: { keys: string[]; label: string }[] = [
  { keys: ["↑", "↓"], label: "軸" },
  { keys: ["←", "→"], label: "ジョグ" },
  { keys: ["[", "]"], label: "量" },
  { keys: ["Shift", "Home", "End"], label: "端" },
];

export function ManualPanel({
  robotKey,
  manual,
  blockedReason,
  sendOrReport,
  excludeAxes,
}: ManualPanelProps) {
  const [picked, setPicked] = useState<string | null>(null);

  const onJog = (axis: string, delta: number) =>
    sendOrReport({ type: "manual_jog", robot: robotKey, axis, delta }, "ジョグ");
  const onSet = (axis: string, value: number) =>
    sendOrReport({ type: "manual_set", robot: robotKey, axis, value }, "目標値の送信");
  const onMove = (axis: string, position: string) =>
    sendOrReport({ type: "manual_move", robot: robotKey, axis, position }, "プリセット移動");

  // 呼び出し元が別の面で持つ軸（吸着パッドの弁）を除く。同じ軸が 2 つの面に並ぶと、
  // どちらを押せば機体が動くのか画面から読めない
  const axes =
    excludeAxes === undefined
      ? manual.axes
      : manual.axes.filter((axis) => !excludeAxes.includes(axis.name));

  const steerable = axes.filter((axis) => axis.manual !== null);
  const { pads, rest: presetOnly } = splitOnOffAxes(axes.filter((axis) => axis.manual === null));
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
      {axes.length === 0 ? (
        <p className="p-2 text-base-content/70">
          このロボットには手動操縦できる軸がありません (位置定数が未読込です)。
        </p>
      ) : (
        <>
          <div className="scroll @container flex min-h-0 flex-1 flex-col">
            {steerable.map((axis) => (
              <ManualAxisRow
                key={axis.name}
                axis={axis}
                blockedReason={blockedReason}
                selected={axis.name === selected}
                onSelect={() => setPicked(axis.name)}
                onJog={onJog}
                onSet={onSet}
                onMove={onMove}
              />
            ))}

            <OnOffPadGroup axes={pads} blockedReason={blockedReason} onMove={onMove} />

            {presetOnly.length === 0 ? null : (
              <div className="grid @min-[40rem]:grid-cols-2 @min-[56rem]:grid-cols-3">
                {presetOnly.map((axis) => (
                  <ManualAxisRow
                    key={axis.name}
                    axis={axis}
                    blockedReason={blockedReason}
                    selected={false}
                    onSelect={() => {}}
                    onJog={onJog}
                    onSet={onSet}
                    onMove={onMove}
                  />
                ))}
              </div>
            )}
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
