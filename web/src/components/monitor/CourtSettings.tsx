import { Info, RotateCcw } from "lucide-react";
import { memo } from "react";

import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { Panel } from "@/components/ui/Panel";
import { useRobotCommands, useRobotStatus } from "@/context/RobotContext";
import { cx } from "@/lib/cx";
import { isDuringMatch, isSetupPhase } from "@/lib/phase";
import type { MatchCourt } from "@/lib/protocol";
import { MALFORMED } from "@/lib/protocol";

const COURT_OPTIONS: { value: MatchCourt; label: string; selectedClass: string }[] = [
  { value: "red", label: "赤コート", selectedClass: "border-error bg-error text-error-content" },
  { value: "blue", label: "青コート", selectedClass: "border-info bg-info text-info-content" },
];

export const CourtSettings = memo(function CourtSettings({
  onRequestReset,
}: {
  onRequestReset: () => void;
}) {
  const { matchState, connected } = useRobotStatus();
  const { setCourt } = useRobotCommands();
  const { court, phase } = matchState;

  const courtLocked = isDuringMatch(phase) || !connected;
  const resettable = isSetupPhase(phase) || phase === "finished";
  const resetLocked = !resettable || !connected || (phase !== "finished" && court === null);

  return (
    <Panel
      legend="コート設定"
      className="shrink-0"
      actions={
        <Button
          disabled={resetLocked}
          onClick={onRequestReset}
          aria-label="試合をリセットしてセッティングタイムへ戻す"
        >
          <Icon as={RotateCcw} />
          RESET
        </Button>
      }
    >
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <div className="join">
          {COURT_OPTIONS.map((opt) => (
            <Button
              key={opt.value}
              className={cx("join-item", court === opt.value && opt.selectedClass)}
              disabled={courtLocked}
              onClick={() => setCourt(opt.value)}
              aria-pressed={court === opt.value}
            >
              {opt.label}
            </Button>
          ))}
        </div>
        {court === null ? (
          <p className="flex items-center gap-1.5 text-[0.9em] text-base-content/70">
            <Icon as={Info} />
            未設定です。選ぶまで試合を開始できません
          </p>
        ) : court === MALFORMED ? (
          <p className="text-[0.9em] text-error">コートの配信を読めていません</p>
        ) : null}
      </div>
    </Panel>
  );
});
