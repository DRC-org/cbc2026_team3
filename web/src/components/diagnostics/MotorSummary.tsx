import { MotorStatHeader, MotorStatus } from "@/components/diagnostics/MotorStatus";
import { ScrollArea } from "@/components/ui/ScrollArea";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { summarizeMotors } from "@/lib/healthVerdict";
import type { TempThresholds } from "@/lib/healthVerdict";
import type { MotorHealth, MotorState } from "@/lib/protocol";

interface MotorSummaryProps {
  motors: Record<string, MotorState>;
  healthMotors?: MotorHealth[];
  tempThresholds?: TempThresholds | null;
}

export function MotorSummary({ motors, healthMotors, tempThresholds = null }: MotorSummaryProps) {
  const total = Object.keys(motors).length;
  const healthMap = Object.fromEntries((healthMotors ?? []).map((m) => [m.name, m]));

  if (total === 0) {
    return <div className="text-base-content/70">モータ情報なし</div>;
  }

  const verdict = summarizeMotors(healthMotors);

  return (
    <div className="@container flex min-h-0 flex-1 flex-col gap-1">
      <div className="flex shrink-0 items-center justify-end gap-2">
        <StatusBadge tone={verdict.tone} title={verdict.label}>
          {verdict.quiet ? null : verdict.label}
        </StatusBadge>
      </div>
      <MotorStatHeader className="shrink-0 border-b border-base-300 pb-[0.1rem]" />
      <ScrollArea className="[&>*:nth-child(odd)]:bg-base-200">
        {Object.entries(motors).map(([name, state]) => (
          <MotorStatus
            key={name}
            name={name}
            state={state}
            health={healthMap[name]}
            tempThresholds={tempThresholds}
          />
        ))}
      </ScrollArea>
    </div>
  );
}
