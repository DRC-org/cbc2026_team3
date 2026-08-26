import { ChevronDown, ChevronRight, ShieldAlert } from "lucide-react";
import { useState } from "react";

import { HealthIndicator } from "@/components/diagnostics/HealthIndicator";
import { MotorSummary } from "@/components/diagnostics/MotorSummary";
import { Icon } from "@/components/ui/Icon";
import { StatusBadge } from "@/components/ui/StatusBadge";
import type { HealthSnapshot, MotorState, SafetyState } from "@/hooks/useRobotSocket";
import { describeSafetyIssues, evaluateHealth } from "@/lib/healthVerdict";

interface SubsystemStatusProps {
  health: HealthSnapshot | undefined;
  motors: Record<string, MotorState>;
  /** 安全機構 (同期ずれラッチ・保護ループの生死)。未受信でも表示は成立する */
  safety?: SafetyState;
  /** 準備中は中身を開いた状態から始める（配線確認が目的のフェーズなので） */
  defaultOpen?: boolean;
  /**
   * 判定チップと開閉見出しを出すか。
   * 同じ画面で別の要素 (StartGate) が既に「異常があるか」を答えている場合は false。
   * 同じ文字列を 2 度並べると、操縦者はどちらが最新か確かめる往復を強いられる。
   */
  showVerdict?: boolean;
}

/**
 * 安全機構の異常。平常時は 1 件も出ない。
 *
 * ラッチ中の軸は緊急停止を解除しても動かず、保護ループが死んでも WS は繋がったまま
 * モータ状態が届き続ける。どちらも「画面が正常に見えるのに機体は正常でない」型の異常で、
 * 自分から主張しない限り誰も気付けない。
 */
function SafetyIssues({ safety }: { safety: SafetyState | undefined }) {
  const issues = describeSafetyIssues(safety);
  if (issues.length === 0) return null;

  return (
    <ul className="flex shrink-0 flex-col gap-1 border-l-[0.25rem] border-l-error bg-error/5 px-2 py-1">
      {issues.map((issue) => (
        <li key={issue.label} className="flex min-w-0 flex-col">
          <span className="flex min-w-0 items-center gap-1.5">
            <Icon as={ShieldAlert} className="shrink-0 text-error" />
            <span className="shrink-0 font-medium">{issue.label}</span>
            <span className="min-w-0 truncate font-mono text-base-content/80">{issue.detail}</span>
          </span>
          {/* 状態だけ出しても操縦者は次の一手を選べない。復旧手順まで書く */}
          <span className="pl-[1.4rem] text-[0.85em] text-base-content/70">{issue.hint}</span>
        </li>
      ))}
    </ul>
  );
}

/**
 * 診断情報の累進的開示。
 *
 * 試合中の操縦者は機体を見ており、画面へ視線を戻すのは一瞬しかない。そこに
 * 8 モータ × 4 値 = 32 個の数字が常時出ていると、本当に必要な「異常があるか」が
 * 数字の海に沈む。しかも試合中にこれらの数値を見て取れる行動は無い。
 * 平常時は 1 行に畳み、異常が出たときだけ自分から開いて主張する。
 */
export function SubsystemStatus({
  health,
  motors,
  safety,
  defaultOpen = false,
  showVerdict = true,
}: SubsystemStatusProps) {
  const verdict = evaluateHealth(health, motors, safety);
  const [manualOpen, setManualOpen] = useState(defaultOpen);

  // 異常時は操縦者の開閉操作より優先して開く。畳んだまま見逃させない
  const forcedOpen = verdict.tone === "error" || verdict.tone === "warning";
  const open = !showVerdict || forcedOpen || manualOpen;

  const busCount = health?.buses.length ?? 0;
  const motorCount = Object.keys(motors).length;

  return (
    <div className="flex min-h-0 flex-col">
      {showVerdict ? (
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
      ) : null}

      {open ? (
        <div className="flex min-h-0 flex-1 flex-col gap-1 pt-1">
          <SafetyIssues safety={safety} />
          <HealthIndicator variant="bus-only" health={health} />
          <MotorSummary motors={motors} healthMotors={health?.motors} />
        </div>
      ) : null}
    </div>
  );
}
