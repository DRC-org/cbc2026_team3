import { Button } from "@/components/ui/Button";
import { commandValueText } from "@/lib/commandValue";
import { cx } from "@/lib/cx";
import type { ManualAxis } from "@/lib/protocol";

const ON_CLASS = "border-success bg-success text-success-content hover:bg-success/85";
// まだ一度も指令していない軸を OFF と同じ見た目にすると、押していないことが画面から消える
const UNKNOWN_CLASS = "border-dashed";

interface OnOffPair {
  on: string;
  off: string;
}

// 位置名は配信された値から引く。UI が open / closed を書き写すと、位置名を変えた日に片方だけ古くなる
function onOffPair(axis: ManualAxis): OnOffPair | null {
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
          <div key={axis.name} className="flex flex-col items-center gap-0.5">
            <Button
              className={cx(
                "h-14 w-14 rounded-full p-0 font-mono",
                on && ON_CLASS,
                axis.target === null && UNKNOWN_CLASS,
              )}
              disabled={blockedReason !== null}
              aria-pressed={on}
              aria-label={`${axis.name} を ${on ? "OFF" : "ON"} にする`}
              onClick={() => onMove(axis.name, on ? pair.off : pair.on)}
            >
              {axis.target === null ? "—" : commandValueText(axis.target, axis.command_mode, 0)}
            </Button>
            <span className="font-mono text-[0.75em]">{axis.name}</span>
          </div>
        );
      })}
    </div>
  );
}
