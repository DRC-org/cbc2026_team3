import { ArrowRight, Ban, Check } from "lucide-react";

import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { Kbd } from "@/components/ui/Kbd";
import type { SequenceKind } from "@/lib/sequenceStatus";

interface TriggerButtonProps {
  kind: SequenceKind;
  onTrigger: () => void;
  disabled?: boolean;
  disabledLabel?: string;
}

const FILL_CLASS = "flex h-full w-full items-center justify-center gap-3 text-[1.4em]";

export function TriggerButton({
  kind,
  onTrigger,
  disabled = false,
  disabledLabel = "試合開始前",
}: TriggerButtonProps) {
  if (disabled) {
    return (
      <Button disabled className={FILL_CLASS} aria-label={`操作不可: ${disabledLabel}`}>
        <Icon as={Ban} />
        {disabledLabel}
      </Button>
    );
  }

  if (kind === "no_sequence") {
    return (
      <Button disabled className={FILL_CLASS} aria-label="操作不可: シーケンス未取得">
        <Icon as={Ban} />
        シーケンス未取得
      </Button>
    );
  }

  if (kind === "complete") {
    return (
      <Button disabled tone="ok" className={FILL_CLASS} aria-label="シーケンス完走">
        <Icon as={Check} />
        DONE
      </Button>
    );
  }

  if (kind === "waiting_trigger") {
    return (
      <Button
        tone="next"
        onClick={onTrigger}
        aria-label="次のステップへ進む"
        className={FILL_CLASS}
      >
        <Icon as={ArrowRight} />
        NEXT
        <Kbd className="bg-next-fg/10 text-next-fg">Space</Kbd>
      </Button>
    );
  }

  return (
    <Button disabled tone="info" className={FILL_CLASS} aria-label="シーケンス実行中">
      <span className="loading loading-sm loading-spinner" />
      RUNNING
    </Button>
  );
}
