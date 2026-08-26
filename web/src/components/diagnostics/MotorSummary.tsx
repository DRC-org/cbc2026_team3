import { MotorStatHeader, MotorStatus } from "@/components/diagnostics/MotorStatus";
import { StatusBadge } from "@/components/ui/StatusBadge";
import type { MotorHealth, MotorState } from "@/hooks/useRobotSocket";
import { countHotMotors } from "@/lib/healthVerdict";

interface MotorSummaryProps {
  motors: Record<string, MotorState>;
  healthMotors?: MotorHealth[];
}

export function MotorSummary({ motors, healthMotors }: MotorSummaryProps) {
  const total = Object.keys(motors).length;
  const anomalyCount = countHotMotors(motors);
  const healthMap = Object.fromEntries((healthMotors ?? []).map((m) => [m.name, m]));

  if (total === 0) {
    return <div className="text-base-content/70">モータ情報なし</div>;
  }

  const hasAnomaly = anomalyCount > 0;

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-1">
      <div className="flex shrink-0 items-center justify-between gap-2">
        <span className="text-base-content/70">{total} 基</span>
        <StatusBadge tone={hasAnomaly ? "warning" : "success"}>
          {hasAnomaly ? `異常 ${anomalyCount} 件` : "All operational"}
        </StatusBadge>
      </div>
      <MotorStatHeader className="shrink-0 border-b border-base-300 pb-[0.1rem]" />
      {/* 縞は交互の地色。等幅の数値が縦に並ぶ表で行を追いやすくする。
          行が 2 段組みで table には収まらないため、変則的な行の縞は変種セレクタで表す */}
      <div className="scroll min-h-0 flex-1 [&>*:nth-child(odd)]:bg-base-200">
        {Object.entries(motors).map(([name, state]) => (
          <MotorStatus key={name} name={name} state={state} health={healthMap[name]} />
        ))}
      </div>
    </div>
  );
}
