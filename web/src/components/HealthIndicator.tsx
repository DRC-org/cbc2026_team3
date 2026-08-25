import { Panel } from "@/components/ui/Panel";
import type { BusHealth, BusHealthState, HealthSnapshot } from "@/hooks/useRobotSocket";
import { cx } from "@/lib/cx";
import type { Tone } from "@/lib/tone";
import { TONE_TEXT_CLASS } from "@/lib/tone";

interface HealthIndicatorProps {
  health: HealthSnapshot | undefined;
  variant?: "pill" | "card" | "compact" | "bus-only";
}

interface ToneStyle {
  label: string;
  symbol: string;
}

const TONE_STYLES: Record<Tone, ToneStyle> = {
  success: { label: "OK", symbol: "✓" },
  warning: { label: "DEGRADED", symbol: "⚠" },
  error: { label: "DOWN", symbol: "✗" },
  info: { label: "INFO", symbol: "·" },
  neutral: { label: "未取得", symbol: "○" },
};

function busTone(state: BusHealthState): Tone {
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

// 状態バッジ（[記号 ラベル]）。
function StatusTag({ tone, extra }: { tone: Tone; extra?: string }) {
  const style = TONE_STYLES[tone];
  return (
    <span className={TONE_TEXT_CLASS[tone]}>
      [{style.symbol} {style.label}
      {extra ? ` ${extra}` : ""}]
    </span>
  );
}

function PillMode({ health }: { health: HealthSnapshot }) {
  const tone = busTone(health.overall);
  const summary = buildSummary(health);
  const tooltip =
    summary.length > 0
      ? `${TONE_STYLES[tone].label}: ${summary}`
      : `${TONE_STYLES[tone].label} (バス ${health.buses.length} / モータ ${health.motors.length})`;
  return (
    <span title={tooltip} aria-label={`ヘルス ${TONE_STYLES[tone].label}`}>
      <StatusTag tone={tone} />
    </span>
  );
}

function CompactMode({ health }: { health: HealthSnapshot }) {
  const tone = busTone(health.overall);
  const summary = buildSummary(health);
  return <StatusTag tone={tone} extra={summary ? `(${summary})` : undefined} />;
}

function BusRow({ bus }: { bus: BusHealth }) {
  const tone = busTone(bus.state);
  const style = TONE_STYLES[tone];
  return (
    <div className="hsplit px-[0.3rem] py-[0.15rem]">
      <span className="hstack items-baseline">
        <span>{bus.name}</span>
        <span className="text-fg-dim">{bus.channel}</span>
      </span>
      <span className={cx("hstack shrink-0 whitespace-nowrap", TONE_TEXT_CLASS[tone])}>
        <span>
          {style.symbol} {style.label}
        </span>
        {bus.bus_off ? <span>bus_off</span> : null}
        {bus.tx_error_count > 0 ? <span>tx_err {bus.tx_error_count}</span> : null}
      </span>
    </div>
  );
}

function BusOnlyMode({ health }: { health: HealthSnapshot }) {
  const tone = busTone(health.overall);
  return (
    <div className="flex flex-col gap-[0.15rem]">
      <div className="hsplit">
        <span className="text-fg-dim">{health.buses.length} 系統</span>
        <StatusTag tone={tone} />
      </div>
      {health.buses.length > 0 ? (
        <div className="striped flex flex-col">
          {health.buses.map((bus) => (
            <BusRow key={bus.name} bus={bus} />
          ))}
        </div>
      ) : (
        <div className="text-fg-dim">バス情報なし</div>
      )}
    </div>
  );
}

function CardMode({ health }: { health: HealthSnapshot }) {
  const tone = busTone(health.overall);
  return (
    <Panel legend="CAN Health">
      <div className="hsplit">
        <span>STATUS</span>
        <StatusTag tone={tone} />
      </div>
      {health.buses.length > 0 ? (
        <div className="striped flex flex-col">
          {health.buses.map((bus) => (
            <BusRow key={bus.name} bus={bus} />
          ))}
        </div>
      ) : null}
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
      <div className="hsplit">
        <span className="text-fg-dim">CAN</span>
        <span className="text-fg-dim">未取得</span>
      </div>
    );
  }
  return (
    <Panel legend="CAN Health">
      <p className="text-fg-dim">ヘルス情報未取得</p>
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
