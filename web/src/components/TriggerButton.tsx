import { Ban, Check, Play } from "lucide-react";

import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { Kbd } from "@/components/ui/Kbd";

interface TriggerButtonProps {
  waiting: boolean;
  stepIndex: number;
  totalSteps: number;
  onTrigger: () => void;
  /** 試合中以外はサーバー側でも拒否されるため、UI でも押せなくする */
  disabled?: boolean;
  disabledLabel?: string;
}

// 試合中に最も多く押すボタンなので、視線を戻した瞬間に状態が読めるよう大きく出す
const FILL_CLASS = "flex h-full w-full items-center justify-center gap-3 text-[1.4em]";

export function TriggerButton({
  waiting,
  stepIndex,
  totalSteps,
  onTrigger,
  disabled = false,
  disabledLabel = "試合開始前",
}: TriggerButtonProps) {
  // バックエンドは完走時 step_index = total_steps を返す。「最終ステップ実行中」と
  // 「全完走」を分けるため、>= total での判定を採用する
  const isComplete = totalSteps > 0 && stepIndex >= totalSteps && !waiting;

  if (disabled) {
    return (
      <Button disabled className={FILL_CLASS} aria-label={`操作不可: ${disabledLabel}`}>
        <Icon as={Ban} />
        {disabledLabel}
      </Button>
    );
  }

  if (isComplete) {
    return (
      <Button disabled tone="ok" className={FILL_CLASS} aria-label="シーケンス完走">
        <Icon as={Check} />
        DONE
      </Button>
    );
  }

  // 待機解除は試合中に最も多く押す操作。ここだけは地をベタ塗りして、
  // 「今 押すべきボタンはこれ」が周辺視野でも分かるようにする
  if (waiting) {
    return (
      <Button
        tone="next"
        onClick={onTrigger}
        aria-label="次のステップへ進む"
        className={FILL_CLASS}
      >
        <Icon as={Play} />
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
