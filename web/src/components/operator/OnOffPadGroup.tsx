import { PadToggle } from "@/components/operator/PadToggle";
import { commandValueText } from "@/lib/commandValue";
import type { ManualAxis } from "@/lib/protocol";

export interface OnOffPair {
  on: string;
  off: string;
}

// 位置名は配信された値から引く。UI が open / closed を書き写すと、位置名を変えた日に片方だけ古くなる
export function onOffPair(axis: ManualAxis): OnOffPair | null {
  if (axis.command_mode !== "on_off") return null;
  const off = axis.positions.find((position) => position.value === 0);
  const on = axis.positions.find((position) => position.value !== 0 && position.value !== null);
  if (off === undefined || on === undefined) return null;
  return { on: on.name, off: off.name };
}

export function splitOnOffAxes(axes: ManualAxis[]): { pads: ManualAxis[]; rest: ManualAxis[] } {
  const pads: ManualAxis[] = [];
  const rest: ManualAxis[] = [];
  for (const axis of axes) {
    if (onOffPair(axis) === null) rest.push(axis);
    else pads.push(axis);
  }
  return { pads, rest };
}

interface OnOffPadGroupProps {
  axes: ManualAxis[];
  blockedReason: string | null;
  onMove: (axis: string, position: string) => void;
}

export function OnOffPadGroup({ axes, blockedReason, onMove }: OnOffPadGroupProps) {
  const pads = axes.flatMap((axis) => {
    const pair = onOffPair(axis);
    return pair === null ? [] : [{ axis, pair }];
  });
  if (pads.length === 0) return null;

  return (
    <div className="flex flex-wrap gap-2 p-2" role="group" aria-label="開閉トグル">
      {pads.map(({ axis, pair }) => {
        const on = axis.target !== null && axis.target !== 0;
        return (
          <PadToggle
            key={axis.name}
            label={axis.target === null ? "—" : commandValueText(axis.target, axis.command_mode, 0)}
            on={on}
            unknown={axis.target === null}
            caption={axis.name}
            disabled={blockedReason !== null}
            ariaLabel={`${axis.name} を ${on ? "OFF" : "ON"} にする`}
            onClick={() => onMove(axis.name, on ? pair.off : pair.on)}
          />
        );
      })}
    </div>
  );
}
