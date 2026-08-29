import { Minus, Plus, Send } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { useHoldRepeat } from "@/hooks/useHoldRepeat";
import { cx } from "@/lib/cx";
import type { ManualAxis } from "@/lib/protocol";

interface ManualAxisRowProps {
  axis: ManualAxis;
  /** 操作できない理由。null なら操作できる */
  blockedReason: string | null;
  onJog: (axis: string, delta: number) => void;
  onSet: (axis: string, value: number) => void;
  onMove: (axis: string, position: string) => void;
}

/** 読めない値の表示。0 で埋めない (測っていない値を測ったように見せない) */
function format(value: number | null, unit: string): string {
  return value === null ? "—" : `${value.toFixed(2)}${unit ? ` ${unit}` : ""}`;
}

/** 現在値の可動範囲内での位置 [%]。範囲外は端で止める */
function ratio(value: number, min: number, max: number): number {
  return Math.min(100, Math.max(0, ((value - min) / (max - min)) * 100));
}

/**
 * 手動操縦の 1 軸。**行の単位は論理軸であってモータではない。**
 *
 * モータ単位の操作面を作ってはならない。左右直結ペア (`y_axis` / `rotate`) が
 * 別々の時刻に動くとその場で機構が壊れるため、指令は必ず軸単位で 1 回だけ出す。
 * ここにモータ名を出しているのは「どの実体が動くか」を示すためだけで、
 * 個別に押せる要素にはしない。
 *
 * 見た目は軸の性格で 3 通りに分かれるが、分岐の根拠はサーバー配信の
 * `manual` と `command_mode` だけで、軸名は一切見ていない (機構が変わって
 * 軸が増減しても、この画面は何も変えなくてよい)。
 */
export function ManualAxisRow({ axis, blockedReason, onJog, onSet, onMove }: ManualAxisRowProps) {
  const range = axis.manual;
  const disabled = blockedReason !== null;

  return (
    <div className="flex flex-col gap-1 border-b border-base-300 px-2 py-1.5 last:border-b-0">
      <div className="flex min-w-0 flex-wrap items-baseline gap-x-3 gap-y-1">
        <span className="min-w-0 shrink-0 font-medium">{axis.name}</span>
        {/* 実体が軸名と同じ単一モータ軸では出さない。同じ語を 2 度描くだけで、
            「同じ事実を 2 度描かない」原則にも反する */}
        {axis.motors.length === 1 && axis.motors[0] === axis.name ? null : (
          <span className="shrink-0 text-[0.8em] text-base-content/45">
            {axis.motors.join(" / ")}
          </span>
        )}
        <span className="ml-auto flex shrink-0 items-baseline gap-3 font-mono tabular-nums">
          <span>
            <span className="text-[0.8em] text-base-content/55">現在 </span>
            {format(axis.value, axis.unit)}
          </span>
          <span className="text-base-content/70">
            <span className="text-[0.8em] text-base-content/55">目標 </span>
            {format(axis.target, axis.unit)}
          </span>
        </span>
      </div>

      {range ? (
        <ContinuousControls
          axis={axis}
          min={range.min}
          max={range.max}
          steps={range.steps}
          disabled={disabled}
          onJog={onJog}
          onSet={onSet}
        />
      ) : null}

      {axis.positions.length > 0 ? (
        <div className="flex flex-wrap items-center gap-1">
          {/* プリセットは位置定数に定義された状態名からしか作らない。
              自由入力を許さないことで「定義した状態以外を送れない」保証が残る */}
          {axis.positions.map((position) => (
            <Button
              key={position}
              disabled={disabled}
              onClick={() => onMove(axis.name, position)}
              aria-label={`${axis.name} を ${position} へ`}
            >
              {position}
            </Button>
          ))}
        </div>
      ) : null}
    </div>
  );
}

interface ContinuousControlsProps {
  axis: ManualAxis;
  min: number;
  max: number;
  steps: number[];
  disabled: boolean;
  onJog: (axis: string, delta: number) => void;
  onSet: (axis: string, value: number) => void;
}

function ContinuousControls({
  axis,
  min,
  max,
  steps,
  disabled,
  onJog,
  onSet,
}: ContinuousControlsProps) {
  const [step, setStep] = useState(steps[0]);
  const [draft, setDraft] = useState("");

  // 端に達したら押しても動かない。理由を画面から読めるようにボタン側で塞ぐ
  // (実際のクランプはサーバーが行う。ここは説明であって判定ではない)
  const anchor = axis.target ?? axis.value;
  const atMin = anchor !== null && anchor <= min;
  const atMax = anchor !== null && anchor >= max;

  const minus = useHoldRepeat(() => onJog(axis.name, -step), !disabled && !atMin);
  const plus = useHoldRepeat(() => onJog(axis.name, step), !disabled && !atMax);

  const submit = () => {
    const value = Number(draft);
    if (draft.trim() === "" || !Number.isFinite(value)) return;
    onSet(axis.name, value);
  };

  return (
    <>
      {/* 可動範囲は**表示専用**。ドラッグできるスライダーにすると、
          触れた瞬間に機体が飛ぶ */}
      <div className="flex items-center gap-2 text-[0.8em] text-base-content/55">
        <span className="w-14 shrink-0 text-right font-mono tabular-nums">{min}</span>
        <div className="relative h-1.5 min-w-0 flex-1 bg-base-200">
          {axis.value === null ? null : (
            <span
              className="absolute top-0 h-full w-0.5 bg-base-content/60"
              style={{ left: `${ratio(axis.value, min, max)}%` }}
              aria-hidden
            />
          )}
          {axis.target === null ? null : (
            <span
              className="absolute top-0 h-full w-0.5 bg-info"
              style={{ left: `${ratio(axis.target, min, max)}%` }}
              aria-hidden
            />
          )}
        </div>
        <span className="w-14 shrink-0 font-mono tabular-nums">{max}</span>
      </div>

      <div className="flex flex-wrap items-center gap-1">
        <Button
          disabled={disabled || atMin}
          aria-label={`${axis.name} を ${step}${axis.unit} 戻す`}
          {...minus}
        >
          <Icon as={Minus} />
        </Button>
        <select
          className="select w-24 border-base-300 bg-base-100 font-mono tabular-nums select-sm"
          aria-label={`${axis.name} のジョグ量`}
          value={step}
          disabled={disabled}
          onChange={(e) => setStep(Number(e.target.value))}
        >
          {steps.map((candidate) => (
            <option key={candidate} value={candidate}>
              {candidate} {axis.unit}
            </option>
          ))}
        </select>
        <Button
          disabled={disabled || atMax}
          aria-label={`${axis.name} を ${step}${axis.unit} 進める`}
          {...plus}
        >
          <Icon as={Plus} />
        </Button>

        <span className="mx-1 h-5 w-px shrink-0 bg-base-300" />

        <input
          type="number"
          inputMode="decimal"
          className={cx(
            "input w-24 border-base-300 bg-base-100 text-right font-mono tabular-nums input-sm",
          )}
          aria-label={`${axis.name} の目標値`}
          placeholder={axis.unit}
          value={draft}
          disabled={disabled}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") submit();
          }}
        />
        <Button
          tone="info"
          disabled={disabled || draft.trim() === ""}
          onClick={submit}
          aria-label={`${axis.name} を入力値へ移動`}
        >
          <Icon as={Send} />
          移動
        </Button>
      </div>
    </>
  );
}
