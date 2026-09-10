import { Button } from "@/components/ui/Button";
import { cx } from "@/lib/cx";

const ON_CLASS = "border-success bg-success text-success-content hover:bg-success/85";
// まだ一度も指令していない軸を OFF と同じ見た目にすると、押していないことが画面から消える
const UNKNOWN_CLASS = "border-dashed";

interface PadToggleProps {
  label: string;
  on: boolean;
  unknown?: boolean;
  caption?: string;
  disabled: boolean;
  ariaLabel: string;
  onClick: () => void;
}

// 弁の宣言（SuctionPadPanel）と今すぐ開閉（OnOffPadGroup）が同じ ON の見た目を共有する。
// 2 箇所に書き写すと、同じ弁が画面の場所によって別の色で見える
export function PadToggle({
  label,
  on,
  unknown = false,
  caption,
  disabled,
  ariaLabel,
  onClick,
}: PadToggleProps) {
  return (
    <div className="flex flex-col items-center gap-0.5">
      <Button
        className={cx(
          "h-14 w-14 rounded-full p-0 font-mono",
          on && ON_CLASS,
          unknown && UNKNOWN_CLASS,
        )}
        disabled={disabled}
        aria-pressed={on}
        aria-label={ariaLabel}
        onClick={onClick}
      >
        {label}
      </Button>
      {caption === undefined ? null : <span className="font-mono text-[0.75em]">{caption}</span>}
    </div>
  );
}
