import { Panel } from "@/components/ui/Panel";
import { StatusBadge } from "@/components/ui/StatusBadge";
import type { BusHealth, BusHealthState, HealthSnapshot } from "@/hooks/useRobotSocket";
import type { Tone } from "@/lib/tone";

interface HealthIndicatorProps {
  health: HealthSnapshot | undefined;
  variant?: "pill" | "card" | "compact" | "bus-only";
}

/** CAN ヘルスは正常/劣化/停止/未取得の 4 段階しか取らない（info は使わない） */
type HealthTone = Exclude<Tone, "info">;

const TONE_LABEL: Record<HealthTone, string> = {
  success: "OK",
  warning: "DEGRADED",
  error: "DOWN",
  neutral: "未取得",
};

function busTone(state: BusHealthState): HealthTone {
  if (state === "ok") return "success";
  if (state === "degraded") return "warning";
  return "error";
}

export function formatAge(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || Number.isNaN(ms)) return "—";
  if (ms < 0) return "—";
  if (ms < 1000) return `${Math.round(ms)}ms 前`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s 前`;
  if (ms < 3_600_000) return `${Math.floor(ms / 60_000)}m 前`;
  return `${Math.floor(ms / 3_600_000)}h 前`;
}

function buildSummary(health: HealthSnapshot): string {
  const badBuses = health.buses.filter((b) => b.state !== "ok");
  const badMotors = health.motors.filter((m) => m.state !== "ok");
  const fragments: string[] = [];
  for (const b of badBuses) fragments.push(`bus ${b.name} ${b.state}`);
  for (const m of badMotors) fragments.push(`motor ${m.name} ${m.state}`);
  return fragments.join(", ");
}

/**
 * daisyUI の table-xs は本文を .6875rem に固定する。ルートの clamp() 由来の
 * 相対サイズから外れて読みづらくなるため、セル側で明示的に上書きする
 * （font-size は tr に当たっているので、td/th の直接指定が勝つ）。
 */
const CELL_CLASS = "text-[0.85em]";

function StatusTag({ tone, extra }: { tone: HealthTone; extra?: string }) {
  return (
    <StatusBadge tone={tone} detail={extra}>
      {TONE_LABEL[tone]}
    </StatusBadge>
  );
}

function PillMode({ health }: { health: HealthSnapshot }) {
  const tone = busTone(health.overall);
  const summary = buildSummary(health);
  const tooltip =
    summary.length > 0
      ? `${TONE_LABEL[tone]}: ${summary}`
      : `${TONE_LABEL[tone]} (バス ${health.buses.length} / モータ ${health.motors.length})`;
  return (
    <StatusBadge tone={tone} title={tooltip}>
      {TONE_LABEL[tone]}
    </StatusBadge>
  );
}

function CompactMode({ health }: { health: HealthSnapshot }) {
  const tone = busTone(health.overall);
  const summary = buildSummary(health);
  return <StatusTag tone={tone} extra={summary ? `(${summary})` : undefined} />;
}

function BusRow({ bus }: { bus: BusHealth }) {
  const tone = busTone(bus.state);
  const notes = [
    bus.bus_off ? "bus_off" : null,
    bus.tx_error_count > 0 ? `tx_err ${bus.tx_error_count}` : null,
  ].filter(Boolean);
  return (
    <tr>
      <td className={`${CELL_CLASS} truncate`}>{bus.name}</td>
      <td className={`${CELL_CLASS} font-mono text-base-content/70`}>{bus.channel}</td>
      <td className={`${CELL_CLASS} text-right`}>
        <StatusBadge tone={tone} detail={notes.length > 0 ? notes.join(" ") : undefined}>
          {TONE_LABEL[tone]}
        </StatusBadge>
      </td>
    </tr>
  );
}

function BusTable({ health }: { health: HealthSnapshot }) {
  if (health.buses.length === 0) {
    return <div className="text-base-content/70">バス情報なし</div>;
  }
  return (
    <table className="table table-zebra table-xs">
      <tbody>
        {health.buses.map((bus) => (
          <BusRow key={bus.name} bus={bus} />
        ))}
      </tbody>
    </table>
  );
}

function BusOnlyMode({ health }: { health: HealthSnapshot }) {
  const tone = busTone(health.overall);
  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-center justify-between gap-2">
        <span className="text-base-content/70">{health.buses.length} 系統</span>
        <StatusTag tone={tone} />
      </div>
      <BusTable health={health} />
    </div>
  );
}

function CardMode({ health }: { health: HealthSnapshot }) {
  const tone = busTone(health.overall);
  return (
    <Panel legend="CAN Health" actions={<StatusTag tone={tone} />}>
      <BusTable health={health} />
    </Panel>
  );
}

function NeutralPlaceholder({
  variant,
}: {
  variant: NonNullable<HealthIndicatorProps["variant"]>;
}) {
  if (variant === "pill" || variant === "compact") {
    return <StatusTag tone="neutral" />;
  }
  if (variant === "bus-only") {
    return (
      <div className="flex items-center justify-between gap-2">
        <span className="text-base-content/70">CAN</span>
        <StatusTag tone="neutral" />
      </div>
    );
  }
  return (
    <Panel legend="CAN Health">
      <p className="text-base-content/70">ヘルス情報未取得</p>
    </Panel>
  );
}

export function HealthIndicator({ health, variant = "compact" }: HealthIndicatorProps) {
  if (!health) return <NeutralPlaceholder variant={variant} />;
  if (variant === "pill") return <PillMode health={health} />;
  if (variant === "compact") return <CompactMode health={health} />;
  if (variant === "bus-only") return <BusOnlyMode health={health} />;
  return <CardMode health={health} />;
}
