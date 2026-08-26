import { ChevronDown, ChevronRight } from "lucide-react";
import { useState } from "react";

import { HealthIndicator } from "@/components/HealthIndicator";
import { MotorSummary } from "@/components/MotorSummary";
import { Icon } from "@/components/ui/Icon";
import { StatusBadge } from "@/components/ui/StatusBadge";
import type { HealthSnapshot, MotorState } from "@/hooks/useRobotSocket";
import { evaluateHealth } from "@/lib/healthVerdict";

interface SubsystemStatusProps {
  health: HealthSnapshot | undefined;
  motors: Record<string, MotorState>;
  /** 準備中は中身を開いた状態から始める（配線確認が目的のフェーズなので） */
  defaultOpen?: boolean;
}

/**
 * 診断情報の累進的開示。
 *
 * 試合中の操縦者は機体を見ており、画面へ視線を戻すのは一瞬しかない。そこに
 * 8 モータ × 4 値 = 32 個の数字が常時出ていると、本当に必要な「異常があるか」が
 * 数字の海に沈む。しかも試合中にこれらの数値を見て取れる行動は無い。
 * 平常時は 1 行に畳み、異常が出たときだけ自分から開いて主張する。
 */
export function SubsystemStatus({ health, motors, defaultOpen = false }: SubsystemStatusProps) {
  const verdict = evaluateHealth(health, motors);
  const [manualOpen, setManualOpen] = useState(defaultOpen);

  // 異常時は操縦者の開閉操作より優先して開く。畳んだまま見逃させない
  const forcedOpen = verdict.tone === "error" || verdict.tone === "warning";
  const open = forcedOpen || manualOpen;

  const busCount = health?.buses.length ?? 0;
  const motorCount = Object.keys(motors).length;

  return (
    <div className="flex min-h-0 flex-col">
      <button
        type="button"
        onClick={() => setManualOpen((v) => !v)}
        aria-expanded={open}
        className="flex shrink-0 cursor-pointer items-center gap-2 px-1 py-1 text-left hover:bg-base-200"
      >
        <Icon as={open ? ChevronDown : ChevronRight} className="text-base-content/60" />
        <StatusBadge tone={verdict.tone}>{verdict.label}</StatusBadge>
        <span className="min-w-0 flex-1 truncate text-base-content/70">
          CAN {busCount} · モータ {motorCount}
        </span>
      </button>

      {open ? (
        <div className="flex min-h-0 flex-1 flex-col gap-1 pt-1">
          <HealthIndicator variant="bus-only" health={health} />
          <MotorSummary motors={motors} healthMotors={health?.motors} />
        </div>
      ) : null}
    </div>
  );
}
