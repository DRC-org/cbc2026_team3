import { Send } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { cx } from "@/lib/cx";
import type { ManualAxis } from "@/lib/protocol";

function toDraft(value: number | null): string {
  return value === null ? "" : String(Number(value.toFixed(3)));
}

interface AbsoluteEntryProps {
  axis: ManualAxis;
  min: number;
  max: number;
  disabled: boolean;
  onSet: (axis: string, value: number) => void;
}

export function AbsoluteEntry({ axis, min, max, disabled, onSet }: AbsoluteEntryProps) {
  const [draft, setDraft] = useState(() => toDraft(axis.target ?? axis.value));
  const dirtyRef = useRef(false);
  const seedRef = useRef<number | null>(null);
  seedRef.current = axis.target ?? axis.value;

  useEffect(() => {
    if (dirtyRef.current) return;
    setDraft(toDraft(seedRef.current));
  }, [axis.target]);

  const parsed = Number(draft);
  const filled = draft.trim() !== "" && Number.isFinite(parsed);
  const outOfRange = filled && (parsed < min || parsed > max);

  const submit = () => {
    if (!filled) return;
    dirtyRef.current = false;
    setDraft(toDraft(seedRef.current));
    onSet(axis.name, parsed);
  };

  return (
    <div className="flex items-center gap-1">
      <input
        type="number"
        inputMode="decimal"
        className={cx(
          "input w-24 border-base-300 bg-base-100 text-right font-mono tabular-nums input-sm",
          outOfRange && "border-warning",
        )}
        aria-label={`${axis.name} の目標値`}
        placeholder={axis.unit}
        value={draft}
        disabled={disabled}
        onChange={(e) => {
          dirtyRef.current = true;
          setDraft(e.target.value);
        }}
        onKeyDown={(e) => {
          if (e.key === "Enter") submit();
        }}
      />
      <Button
        tone="info"
        disabled={disabled || !filled}
        onClick={submit}
        aria-label={`${axis.name} を入力値へ移動`}
      >
        <Icon as={Send} />
        移動
      </Button>
      {outOfRange ? (
        <span className="text-[0.8em] text-warning">
          範囲外 — {parsed < min ? min : max} {axis.unit} へ丸めます
        </span>
      ) : null}
    </div>
  );
}
