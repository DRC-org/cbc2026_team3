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

/**
 * 押し続けたときに 1 回の量を何倍まで伸ばしてよいか。
 *
 * **可動域の 1/8 を 1 回で飛び越えない**ことを上限に置く。ベンチの `y_axis` は
 * 可動範囲 370mm / 刻み 10mm で、等倍のままでは端から端まで 37 回の押下が要る
 * (押しっぱなしでも 3 秒以上)。一方で刻みが青天井に伸びると、離す直前の 1 回だけで
 * 機構を端まで運んでしまう。2 の冪へ落とすのは、伸びた量を `×2` `×4` として
 * 画面に出したときに操縦者が暗算できる形にするため。
 */
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
  // **インデックスで扱う。** かつては `<option value={候補の数値}>` に戻して
  // `steps.indexOf(Number(...))` で引き直していたが、config の `steps` に浮動小数
  // (0.05 等) が入ると往復変換で一致せず `indexOf` が -1 を返し、刻みが**黙って 1**
  // へ落ちた。症状は「選んだ量と違う量で動く」だけで、config にも画面にも痕跡が残らない。
  // 可動範囲と刻みは config が宣言する境界であって、UI が値を捏造してよい所ではない
  const [stepIndex, setStepIndex] = useState(0);
  const step = steps[Math.min(stepIndex, steps.length - 1)];
  const maxMultiplier = maxMultiplierFor(min, max, step);

  // 端に達したら押しても動かない。理由を画面から読めるようにボタン側で塞ぐ
  // (実際のクランプはサーバーが行う。ここは説明であって判定ではない)
  const anchor = axis.target ?? axis.value;
  const atMin = anchor !== null && anchor <= min;
  const atMax = anchor !== null && anchor >= max;

  const canMinus = !disabled && !atMin;
  const canPlus = !disabled && !atMax;
  const jog = (sign: 1 | -1) => (multiplier: number) => onJog(axis.name, sign * step * multiplier);

  const minus = useHoldRepeat(jog(-1), canMinus, maxMultiplier);
  const plus = useHoldRepeat(jog(1), canPlus, maxMultiplier);
  // キーボードは選択中の行だけが張る。押しっぱなしの加速はポインタと同じ engine
  const keyMinus = useHoldKey("ArrowLeft", jog(-1), selected && canMinus, maxMultiplier);
  const keyPlus = useHoldKey("ArrowRight", jog(1), selected && canPlus, maxMultiplier);

  const minusBoost = Math.max(minus.multiplier, keyMinus.multiplier);
  const plusBoost = Math.max(plus.multiplier, keyPlus.multiplier);

  useHotkeys(
    {
      "[": () => setStepIndex((i) => Math.max(0, i - 1)),
      "]": () => setStepIndex((i) => Math.min(steps.length - 1, i + 1)),
      Home: () => onSet(axis.name, min),
      End: () => onSet(axis.name, max),
    },
    selected && !disabled,
  );

  return (
    <>
      <RangeBar axis={axis} min={min} max={max} />

      {/* クラスタ単位で折り返す。要素ごとに折り返させると、軸によって
          「+ が右端にある行」と「+ が次の行の左端にある行」が混在し、
          同じ操作を毎回探し直すことになる */}
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

/**
 * 押し続けて伸びた実効量。**伸びていないときは何も出さない** ——
 * 常時 `×1` を出すと、平常時の行に意味の無い記号が 2 つ増える。
 */
function Boost({ multiplier }: { multiplier: number }) {
  if (multiplier <= 1) return null;
  return <span className="font-mono text-[0.8em] tabular-nums">×{multiplier}</span>;
}
