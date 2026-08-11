import { MotorStatus } from "@/components/MotorStatus";
import type { MotorHealth, MotorState } from "@/hooks/useRobotSocket";
import { cx } from "@/lib/cx";
import { TEMP_WARNING } from "@/lib/robots";

interface MotorSummaryProps {
  motors: Record<string, MotorState>;
  healthMotors?: MotorHealth[];
}

function countAnomalies(motors: Record<string, MotorState>): number {
  return Object.values(motors).filter((m) => m.temp >= TEMP_WARNING).length;
}

function SummaryBadge({ hasAnomaly, anomalyCount }: { hasAnomaly: boolean; anomalyCount: number }) {
  return (
    <span
      className={cx(hasAnomaly ? "warning-text" : "success-text")}
      style={{ whiteSpace: "nowrap" }}
    >
      [{hasAnomaly ? "⚠" : "✓"} {hasAnomaly ? `異常 ${anomalyCount} 件` : `All operational`}]
    </span>
  );
}

export function MotorSummary({ motors, healthMotors }: MotorSummaryProps) {
  const total = Object.keys(motors).length;
  const anomalyCount = countAnomalies(motors);
  const healthMap = Object.fromEntries((healthMotors ?? []).map((m) => [m.name, m]));

  if (total === 0) {
    return <div style={{ padding: 8, opacity: 0.7 }}>モータ情報なし</div>;
  }

  const hasAnomaly = anomalyCount > 0;

  return (
    <div style={{ display: "flex", flexDirection: "column", flex: 1, gap: 4 }}>
      <div style={{ display: "flex", flexShrink: 0, justifyContent: "flex-end" }}>
        <SummaryBadge hasAnomaly={hasAnomaly} anomalyCount={anomalyCount} />
      </div>
      <div className="tui-scroll-cyan" style={{ overflow: "auto" }}>
        <div style={{ display: "flex", flexDirection: "column" }}>
          {Object.entries(motors).map(([name, state]) => (
            <MotorStatus key={name} name={name} state={state} health={healthMap[name]} />
          ))}
        </div>
      </div>
    </div>
  );
}
