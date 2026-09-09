import {
  ChevronDown,
  ChevronRight,
  Fence,
  ListX,
  PackageX,
  ShieldAlert,
  ShieldOff,
  ShieldQuestion,
} from "lucide-react";
import { useEffect, useId, useState } from "react";

import { HealthIndicator } from "@/components/diagnostics/HealthIndicator";
import { MotorSummary } from "@/components/diagnostics/MotorSummary";
import { SensorSummary } from "@/components/diagnostics/SensorSummary";
import type { SensorPayload } from "@/components/diagnostics/SensorSummary";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { cx } from "@/lib/cx";
import {
  describeSafetyIssues,
  evaluateHealth,
  failedTasks,
  firmwareUnconfirmedMotors,
  isReenergizePending,
  limitBlindSensors,
  limitLatchedAxes,
  readableHealth,
  workpieceRiskBuses,
} from "@/lib/healthVerdict";
import type {
  HealthPayload,
  LimitLatchedAxis,
  SafetyPayload,
  TempThresholds,
} from "@/lib/healthVerdict";
import type { BusHealth, MotorState } from "@/lib/protocol";

interface SubsystemStatusProps {
  health: HealthPayload | undefined;
  motors: Record<string, MotorState>;
  safety?: SafetyPayload;
  sensors?: SensorPayload;
  tempThresholds?: TempThresholds | null;
  connected: boolean;
  defaultOpen?: boolean;
  showVerdict?: boolean;
  onReenergize?: () => void;
}

function WorkpieceRiskNotice({ buses }: { buses: BusHealth[] }) {
  if (buses.length === 0) return null;

  return (
    <ul
      aria-label="ワーク落下の恐れがあるバス"
      className="flex shrink-0 flex-col gap-1 border-l-[0.25rem] border-l-warning bg-warning/5 px-2 py-1"
    >
      {buses.map((bus) => (
        <li key={bus.name} className="flex min-w-0 flex-col">
          <span className="flex min-w-0 items-center gap-1.5">
            <Icon as={PackageX} className="shrink-0 text-warning" />
            <StatusBadge tone="warning">CAN 途絶 {bus.rx_down_episodes}回</StatusBadge>
            <span className="min-w-0 truncate font-mono text-base-content/80">{bus.name}</span>
          </span>
          <span className="pl-[1.4rem] text-[0.85em] text-base-content/70">
            吸着していたワークが落ちた可能性があります (基板のコマンドウォッチドッグが満了)
          </span>
        </li>
      ))}
    </ul>
  );
}

function FirmwareUnconfirmedNotice({ motors }: { motors: string[] }) {
  if (motors.length === 0) return null;

  return (
    <ul
      aria-label="版番号 未確認のモータ"
      className="flex shrink-0 flex-col gap-1 border-l-[0.25rem] border-l-info bg-info/5 px-2 py-1"
    >
      {motors.map((motor) => (
        <li key={motor} className="flex min-w-0 flex-col">
          <span className="flex min-w-0 items-center gap-1.5">
            <Icon as={ShieldQuestion} className="shrink-0 text-info" />
            <StatusBadge tone="info">版番号 未確認</StatusBadge>
            <span className="min-w-0 truncate font-mono text-base-content/80">{motor}</span>
          </span>
          <span className="pl-[1.4rem] text-[0.85em] text-base-content/70">
            FEEDBACK は届くのに INFO が来ません。ファームを焼き直して candump で確認してください
          </span>
        </li>
      ))}
    </ul>
  );
}

function LimitLatchedNotice({ axes }: { axes: LimitLatchedAxis[] }) {
  if (axes.length === 0) return null;

  return (
    <ul
      aria-label="リミット到達の軸"
      className="flex shrink-0 flex-col gap-1 border-l-[0.25rem] border-l-warning bg-warning/5 px-2 py-1"
    >
      {axes.map((latched) => (
        <li key={latched.axis} className="flex min-w-0 flex-col">
          <span className="flex min-w-0 items-center gap-1.5">
            <Icon as={Fence} className="shrink-0 text-warning" />
            <StatusBadge tone="warning">リミット到達</StatusBadge>
            <span className="min-w-0 truncate font-mono text-base-content/80">{latched.axis}</span>
          </span>
          <span className="pl-[1.4rem] text-[0.85em] text-base-content/70">
            {latched.sensors.join(", ")} に接触。この向きへの指令は止まります (逆向きへは動きます)
          </span>
        </li>
      ))}
    </ul>
  );
}

function LimitBlindNotice({ sensors }: { sensors: string[] }) {
  if (sensors.length === 0) return null;

  return (
    <ul
      aria-label="リミット保護が無効なセンサ"
      className="flex shrink-0 flex-col gap-1 border-l-[0.25rem] border-l-info bg-info/5 px-2 py-1"
    >
      {sensors.map((sensor) => (
        <li key={sensor} className="flex min-w-0 flex-col">
          <span className="flex min-w-0 items-center gap-1.5">
            <Icon as={ShieldOff} className="shrink-0 text-info" />
            <StatusBadge tone="info">リミット保護 無効</StatusBadge>
            <span className="min-w-0 truncate font-mono text-base-content/80">{sensor}</span>
          </span>
          <span className="pl-[1.4rem] text-[0.85em] text-base-content/70">
            このスイッチは応答が途絶えており、触れても軸は止まりません
          </span>
        </li>
      ))}
    </ul>
  );
}

function FailedTasksNotice({ labels }: { labels: string[] }) {
  if (labels.length === 0) return null;

  return (
    <ul
      aria-label="失敗したタスク"
      className="flex shrink-0 flex-col gap-1 border-l-[0.25rem] border-l-info bg-info/5 px-2 py-1"
    >
      {labels.map((label) => (
        <li key={label} className="flex min-w-0 flex-col">
          <span className="flex min-w-0 items-center gap-1.5">
            <Icon as={ListX} className="shrink-0 text-info" />
            <StatusBadge tone="info">タスク失敗</StatusBadge>
            <span className="min-w-0 truncate text-base-content/80">{label}</span>
          </span>
          <span className="pl-[1.4rem] text-[0.85em] text-base-content/70">
            再起動ではなく journal (journalctl -u cbc-control) で原因を確認してください
          </span>
        </li>
      ))}
    </ul>
  );
}

function SafetyIssues({
  safety,
  onReenergize,
  verdictShown,
}: {
  safety: SafetyPayload | undefined;
  onReenergize?: () => void;
  verdictShown: boolean;
}) {
  const issues = describeSafetyIssues(safety);
  const pending = isReenergizePending(safety);
  if (issues.length === 0) return null;

  return (
    <ul className="flex shrink-0 flex-col gap-1 border-l-[0.25rem] border-l-error bg-error/5 px-2 py-1">
      {issues.map((issue, index) => {
        const restated = verdictShown && index === 0;
        return (
          <li key={issue.label} className="flex min-w-0 flex-col">
            {restated ? null : (
              <span className="flex min-w-0 items-center gap-1.5">
                <Icon as={ShieldAlert} className="shrink-0 text-error" />
                <span className="shrink-0 font-medium">{issue.label}</span>
                <span className="min-w-0 truncate font-mono text-base-content/80">
                  {issue.detail}
                </span>
              </span>
            )}
            <span
              className={cx("text-[0.85em] text-base-content/70", restated ? null : "pl-[1.4rem]")}
            >
              {issue.hint}
            </span>
            {issue.kind === "unenergized" && onReenergize ? (
              <Button
                tone="warn"
                className={cx("self-start", restated ? null : "ml-[1.4rem]")}
                onClick={onReenergize}
                disabled={pending}
              >
                {pending ? "処理中…" : "再励磁"}
              </Button>
            ) : null}
          </li>
        );
      })}
    </ul>
  );
}

export function SubsystemStatus({
  health,
  motors,
  safety,
  sensors,
  connected,
  tempThresholds = null,
  defaultOpen = false,
  showVerdict = true,
  onReenergize,
}: SubsystemStatusProps) {
  const verdict = evaluateHealth(health, safety, connected);
  const readable = readableHealth(health);
  const riskyBuses = workpieceRiskBuses(health);
  const unconfirmedMotors = firmwareUnconfirmedMotors(safety);
  const latchedAxes = limitLatchedAxes(safety);
  const blindSensors = limitBlindSensors(safety);
  const failedTaskLabels = failedTasks(safety);
  const [manualOpen, setManualOpen] = useState(defaultOpen);
  useEffect(() => setManualOpen(defaultOpen), [defaultOpen]);
  const detailsId = useId();

  const forcedOpen =
    verdict.tone === "error" ||
    verdict.tone === "warning" ||
    riskyBuses.length > 0 ||
    latchedAxes.length > 0;
  const open = !showVerdict || forcedOpen || manualOpen;

  const busCount = readable?.buses.length ?? 0;
  const motorCount = Object.keys(motors).length;

  return (
    <div className="flex min-h-0 flex-col">
      {showVerdict ? (
        <button
          type="button"
          onClick={() => setManualOpen(!open)}
          aria-expanded={open}
          aria-controls={detailsId}
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
        <div id={detailsId} className="flex min-h-0 flex-1 flex-col gap-1 pt-1">
          {verdict.detail ? (
            <p className="shrink-0 border-l-[0.25rem] border-l-error bg-error/5 px-2 py-1">
              {verdict.detail}
            </p>
          ) : null}
          <WorkpieceRiskNotice buses={riskyBuses} />
          <LimitLatchedNotice axes={latchedAxes} />
          <FirmwareUnconfirmedNotice motors={unconfirmedMotors} />
          <LimitBlindNotice sensors={blindSensors} />
          <FailedTasksNotice labels={failedTaskLabels} />
          <SafetyIssues safety={safety} onReenergize={onReenergize} verdictShown={showVerdict} />
          <HealthIndicator health={readable} />
          <SensorSummary sensors={sensors} />
          <MotorSummary
            motors={motors}
            healthMotors={readable?.motors}
            tempThresholds={tempThresholds}
          />
        </div>
      ) : null}
    </div>
  );
}
