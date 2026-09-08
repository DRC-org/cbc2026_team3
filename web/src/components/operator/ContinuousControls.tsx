import { ChevronsLeft, ChevronsRight, Minus, Plus } from "lucide-react";
import { useState } from "react";

import { AbsoluteEntry } from "@/components/operator/AbsoluteEntry";
import { RangeBar } from "@/components/operator/RangeBar";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { useHoldKey } from "@/hooks/useHoldKey";
import { useHoldRepeat } from "@/hooks/useHoldRepeat";
import { useHotkeys } from "@/hooks/useHotkeys";
import type { ManualAxis } from "@/lib/protocol";

interface ContinuousControlsProps {
  axis: ManualAxis;
  min: number;
  max: number;
  steps: number[];
  disabled: boolean;
  selected: boolean;
  onJog: (axis: string, delta: number) => void;
  onSet: (axis: string, value: number) => void;
}

function maxMultiplierFor(min: number, max: number, step: number): number {
  const limit = (max - min) / 8 / step;
  if (!Number.isFinite(limit) || limit < 2) return 1;
  return 2 ** Math.floor(Math.log2(limit));
}

export function ContinuousControls({
  axis,
  min,
  max,
  steps,
  disabled,
  selected,
  onJog,
  onSet,
}: ContinuousControlsProps) {
  const [stepIndex, setStepIndex] = useState(0);
  const step = steps[Math.min(stepIndex, steps.length - 1)];
  const maxMultiplier = maxMultiplierFor(min, max, step);

  const anchor = axis.target ?? axis.value;
  const atMin = anchor !== null && anchor <= min;
  const atMax = anchor !== null && anchor >= max;

  const canMinus = !disabled && !atMin;
  const canPlus = !disabled && !atMax;
  const jog = (sign: 1 | -1) => (multiplier: number) => onJog(axis.name, sign * step * multiplier);

  const minus = useHoldRepeat(jog(-1), canMinus, maxMultiplier);
  const plus = useHoldRepeat(jog(1), canPlus, maxMultiplier);
  const keyMinus = useHoldKey("ArrowLeft", jog(-1), selected && canMinus, maxMultiplier);
  const keyPlus = useHoldKey("ArrowRight", jog(1), selected && canPlus, maxMultiplier);

  const minusBoost = Math.max(minus.multiplier, keyMinus.multiplier);
  const plusBoost = Math.max(plus.multiplier, keyPlus.multiplier);

  useHotkeys(
    {
      "[": () => setStepIndex((i) => Math.max(0, i - 1)),
      "]": () => setStepIndex((i) => Math.min(steps.length - 1, i + 1)),
      "Shift+Home": () => onSet(axis.name, min),
      "Shift+End": () => onSet(axis.name, max),
    },
    selected && !disabled,
  );

  return (
    <>
      <RangeBar axis={axis} min={min} max={max} />

      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <div className="flex items-center gap-1">
          <Button
            disabled={disabled || atMin}
            onClick={() => onSet(axis.name, min)}
            aria-label={`${axis.name} を下限 ${min}${axis.unit} へ`}
            title={`下限 ${min} ${axis.unit}`}
          >
            <Icon as={ChevronsLeft} />
          </Button>
          <Button
            disabled={!canMinus}
            aria-label={`${axis.name} を ${step * minusBoost}${axis.unit} 戻す`}
            {...minus.handlers}
          >
            <Icon as={Minus} />
            <Boost multiplier={minusBoost} />
          </Button>
          <select
            className="select w-24 border-base-300 bg-base-100 font-mono tabular-nums select-sm"
            aria-label={`${axis.name} のジョグ量`}
            value={stepIndex}
            disabled={disabled}
            onChange={(e) => setStepIndex(Number(e.target.value))}
          >
            {steps.map((candidate, index) => (
              <option key={candidate} value={index}>
                {candidate} {axis.unit}
              </option>
            ))}
          </select>
          <Button
            disabled={!canPlus}
            aria-label={`${axis.name} を ${step * plusBoost}${axis.unit} 進める`}
            {...plus.handlers}
          >
            <Icon as={Plus} />
            <Boost multiplier={plusBoost} />
          </Button>
          <Button
            disabled={disabled || atMax}
            onClick={() => onSet(axis.name, max)}
            aria-label={`${axis.name} を上限 ${max}${axis.unit} へ`}
            title={`上限 ${max} ${axis.unit}`}
          >
            <Icon as={ChevronsRight} />
          </Button>
        </div>

        <AbsoluteEntry axis={axis} min={min} max={max} disabled={disabled} onSet={onSet} />
      </div>
    </>
  );
}

function Boost({ multiplier }: { multiplier: number }) {
  if (multiplier <= 1) return null;
  return <span className="font-mono text-[0.8em] tabular-nums">×{multiplier}</span>;
}
