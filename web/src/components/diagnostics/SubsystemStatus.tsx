import {
  ChevronDown,
  ChevronRight,
  ListX,
  PackageX,
  ShieldAlert,
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
  readableHealth,
  workpieceRiskBuses,
} from "@/lib/healthVerdict";
import type { HealthPayload, SafetyPayload, TempThresholds } from "@/lib/healthVerdict";
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
  /** 操縦画面向け。復旧手順の文を落としてチップだけにする（機体を見る時間を文章に取られない） */
  concise?: boolean;
  onReenergize?: () => void;
}

function WorkpieceRiskNotice({ buses, concise }: { buses: BusHealth[]; concise: boolean }) {
  if (buses.length === 0) return null;

  return (
    <ul className="flex shrink-0 flex-col gap-1 border-l-[0.25rem] border-l-warning bg-warning/5 px-2 py-1">
      {buses.map((bus) => (
        <li key={bus.name} className="flex min-w-0 flex-col">
          <span className="flex min-w-0 items-center gap-1.5">
            <Icon as={PackageX} className="shrink-0 text-warning" />
            <StatusBadge tone="warning">CAN 途絶 {bus.rx_down_episodes}回</StatusBadge>
            <span className="min-w-0 truncate font-mono text-base-content/80">{bus.name}</span>
          </span>
          {concise ? null : (
            <span className="pl-[1.4rem] text-[0.85em] text-base-content/70">
              吸着していたワークが落ちた可能性があります (基板のコマンドウォッチドッグが満了)
            </span>
          )}
        </li>
      ))}
    </ul>
  );
}

function FirmwareUnconfirmedNotice({ motors, concise }: { motors: string[]; concise: boolean }) {
  if (motors.length === 0) return null;

  return (
    <ul className="flex shrink-0 flex-col gap-1 border-l-[0.25rem] border-l-info bg-info/5 px-2 py-1">
      {motors.map((motor) => (
        <li key={motor} className="flex min-w-0 flex-col">
          <span className="flex min-w-0 items-center gap-1.5">
            <Icon as={ShieldQuestion} className="shrink-0 text-info" />
            <StatusBadge tone="info">版番号 未確認</StatusBadge>
            <span className="min-w-0 truncate font-mono text-base-content/80">{motor}</span>
          </span>
          {concise ? null : (
            <span className="pl-[1.4rem] text-[0.85em] text-base-content/70">
              FEEDBACK は届くのに INFO が来ません。ファームを焼き直して candump で確認してください
            </span>
          )}
        </li>
      ))}
    </ul>
  );
}

function FailedTasksNotice({ labels, concise }: { labels: string[]; concise: boolean }) {
  if (labels.length === 0) return null;

  return (
    <ul className="flex shrink-0 flex-col gap-1 border-l-[0.25rem] border-l-info bg-info/5 px-2 py-1">
      {labels.map((label) => (
        <li key={label} className="flex min-w-0 flex-col">
          <span className="flex min-w-0 items-center gap-1.5">
            <Icon as={ListX} className="shrink-0 text-info" />
            <StatusBadge tone="info">タスク失敗</StatusBadge>
            <span className="min-w-0 truncate text-base-content/80">{label}</span>
          </span>
          {concise ? null : (
            <span className="pl-[1.4rem] text-[0.85em] text-base-content/70">
              再起動ではなく journal (journalctl -u cbc-control) で原因を確認してください
            </span>
          )}
        </li>
      ))}
    </ul>
  );
}

function SafetyIssues({
  safety,
  onReenergize,
  verdictShown,
  concise,
}: {
  safety: SafetyPayload | undefined;
  onReenergize?: () => void;
  verdictShown: boolean;
  concise: boolean;
}) {
  const pending = isReenergizePending(safety);
  const rows = describeSafetyIssues(safety).map((issue, index) => ({
    issue,
    restated: verdictShown && index === 0,
    reenergize: issue.kind === "unenergized" ? onReenergize : undefined,
  }));
  // 見出しも手順もボタンも無い行は、赤い帯だけが残って何も言わない
  const visible = rows.filter((row) => !(row.restated && concise && row.reenergize === undefined));
  if (visible.length === 0) return null;

  return (
    <ul className="flex shrink-0 flex-col gap-1 border-l-[0.25rem] border-l-error bg-error/5 px-2 py-1">
      {visible.map(({ issue, restated, reenergize }) => (
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
          {concise ? null : (
            <span
              className={cx("text-[0.85em] text-base-content/70", restated ? null : "pl-[1.4rem]")}
            >
              {issue.hint}
            </span>
          )}
          {reenergize ? (
            <Button
              tone="warn"
              className={cx("self-start", restated ? null : "ml-[1.4rem]")}
              onClick={reenergize}
              disabled={pending}
            >
              {pending ? "処理中…" : "再励磁"}
            </Button>
          ) : null}
        </li>
      ))}
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
  concise = false,
  onReenergize,
}: SubsystemStatusProps) {
  const verdict = evaluateHealth(health, safety, connected);
  const readable = readableHealth(health);
  const riskyBuses = workpieceRiskBuses(health);
  const unconfirmedMotors = firmwareUnconfirmedMotors(safety);
  const failedTaskLabels = failedTasks(safety);
  const [manualOpen, setManualOpen] = useState(defaultOpen);
  useEffect(() => setManualOpen(defaultOpen), [defaultOpen]);
  const detailsId = useId();

  const forcedOpen =
    verdict.tone === "error" || verdict.tone === "warning" || riskyBuses.length > 0;
  const open = !showVerdict || forcedOpen || manualOpen;

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
          <StatusBadge tone={verdict.tone} title={verdict.label} className="min-w-0">
            {verdict.quiet ? null : verdict.label}
          </StatusBadge>
        </button>
      ) : null}

      {open ? (
        <div id={detailsId} className="flex min-h-0 flex-1 flex-col gap-1 pt-1">
          {verdict.detail && !concise ? (
            <p className="shrink-0 border-l-[0.25rem] border-l-error bg-error/5 px-2 py-1">
              {verdict.detail}
            </p>
          ) : null}
          <WorkpieceRiskNotice buses={riskyBuses} concise={concise} />
          <FirmwareUnconfirmedNotice motors={unconfirmedMotors} concise={concise} />
          <FailedTasksNotice labels={failedTaskLabels} concise={concise} />
          <SafetyIssues
            safety={safety}
            onReenergize={onReenergize}
            verdictShown={showVerdict}
            concise={concise}
          />
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
