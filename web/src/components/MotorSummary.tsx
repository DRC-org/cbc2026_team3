import { MotorStatus } from "@/components/MotorStatus";
import type { MotorHealth, MotorState } from "@/hooks/useRobotSocket";
import { TEMP_WARNING } from "@/lib/robots";

interface MotorSummaryProps {
  motors: Record<string, MotorState>;
  healthMotors?: MotorHealth[];
}

function countAnomalies(motors: Record<string, MotorState>): number {
  return Object.values(motors).filter((m) => m.temp >= TEMP_WARNING).length;
}

export function MotorSummary({ motors, healthMotors }: MotorSummaryProps) {
  const total = Object.keys(motors).length;
  const anomalyCount = countAnomalies(motors);
  const healthMap = Object.fromEntries((healthMotors ?? []).map((m) => [m.name, m]));

  if (total === 0) {
    return <div className="text-fg-dim">モータ情報なし</div>;
  }

  const hasAnomaly = anomalyCount > 0;

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-1">
      <div className="hsplit shrink-0">
        <span className="text-fg-dim">{total} 基</span>
        <span
          className={
            hasAnomaly ? "whitespace-nowrap text-warning" : "whitespace-nowrap text-success"
          }
        >
          [{hasAnomaly ? `⚠ 異常 ${anomalyCount} 件` : "✓ All operational"}]
        </span>
      </div>
      <div className="panel-body scroll striped">
        {Object.entries(motors).map(([name, state]) => (
          <MotorStatus key={name} name={name} state={state} health={healthMap[name]} />
        ))}
      </div>
    </div>
  );
}
