import { Button } from "@tsaito18/tuicss-react";
import { useEffect, useState } from "react";

interface TriggerButtonProps {
  waiting: boolean;
  stepIndex: number;
  totalSteps: number;
  onTrigger: () => void;
  /** 試合中以外はサーバー側でも拒否されるため、UI でも押せなくする */
  disabled?: boolean;
  disabledLabel?: string;
}

// 実行中表示用の ASCII 回転記号。lucide スピナー撤去の代替（CSS keyframe 不要）。
const SPINNER_FRAMES = ["|", "/", "-", "\\"];

function useAsciiSpinner(active: boolean): string {
  const [frame, setFrame] = useState(0);
  useEffect(() => {
    if (!active) return;
    const id = setInterval(() => setFrame((f) => (f + 1) % SPINNER_FRAMES.length), 120);
    return () => clearInterval(id);
  }, [active]);
  return SPINNER_FRAMES[frame];
}

// 試合中に最も多く押すボタンなので、視線を戻した瞬間に状態が読めるよう大きく出す
const FILL_STYLE = {
  display: "flex",
  width: "100%",
  alignItems: "center",
  justifyContent: "center",
  gap: "0.75rem",
  height: "100%",
  fontSize: "1.4em",
} as const;

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
  const spinner = useAsciiSpinner(!disabled && !waiting && !isComplete);

  if (disabled) {
    return (
      <Button disabled style={FILL_STYLE} aria-label={`操作不可: ${disabledLabel}`}>
        ⊘ {disabledLabel}
      </Button>
    );
  }

  if (isComplete) {
    return (
      <Button disabled className="green-255" style={FILL_STYLE} aria-label="シーケンス完走">
        ✓ DONE
      </Button>
    );
  }

  if (waiting) {
    return (
      <Button
        className="blue-255"
        onClick={onTrigger}
        aria-label="次のステップへ進む"
        style={FILL_STYLE}
      >
        ► NEXT
        <span className="key-hint">Space</span>
      </Button>
    );
  }

  return (
    <Button disabled className="cyan-255" style={FILL_STYLE} aria-label="シーケンス実行中">
      <span className="tabular-nums">[{spinner}]</span>
      RUNNING
    </Button>
  );
}
