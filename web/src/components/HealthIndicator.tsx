import type { BusHealth, BusHealthState, HealthSnapshot } from "@/hooks/useRobotSocket";

interface HealthIndicatorProps {
  health: HealthSnapshot | undefined;
  variant?: "pill" | "card" | "compact" | "bus-only";
}

type Tone = "success" | "warning" | "danger" | "neutral";

interface ToneStyle {
  label: string;
  symbol: string;
  // TuiCss セマンティック text クラス。
  textClass: string;
}

const TONE_STYLES: Record<Tone, ToneStyle> = {
  success: { label: "OK", symbol: "✓", textClass: "success-text" },
  warning: { label: "DEGRADED", symbol: "⚠", textClass: "warning-text" },
  danger: { label: "DOWN", symbol: "✗", textClass: "danger-text" },
  neutral: { label: "未取得", symbol: "○", textClass: "secondary-text" },
};

function busTone(state: BusHealthState): Tone {
  if (state === "ok") return "success";
  if (state === "degraded") return "warning";
  return "danger";
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

// 状態バッジ（[記号 ラベル]）。色は TuiCss text クラスで付与。
function StatusTag({ tone, extra }: { tone: Tone; extra?: string }) {
  const style = TONE_STYLES[tone];
  return (
    <span className={style.textClass}>
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
  const style = TONE_STYLES[busTone(bus.state)];
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        gap: "0.75rem",
        padding: "0.25rem",
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: "0.5rem",
          minWidth: 0,
        }}
      >
        <span>{bus.name}</span>
        <span style={{ opacity: 0.6 }}>{bus.channel}</span>
      </div>
      <div
        className={style.textClass}
        style={{
          display: "flex",
          alignItems: "center",
          gap: "0.5rem",
        }}
      >
        <span>
          {style.symbol} {style.label}
        </span>
        {bus.bus_off ? <span>bus_off</span> : null}
        {bus.tx_error_count > 0 ? (
          <span style={{ opacity: 0.8 }}>tx_err {bus.tx_error_count}</span>
        ) : null}
      </div>
    </div>
  );
}

function BusOnlyMode({ health }: { health: HealthSnapshot }) {
  const tone = busTone(health.overall);
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "0.25rem" }}>
      <div style={{ display: "flex", justifyContent: "flex-end" }}>
        <StatusTag tone={tone} />
      </div>
      {health.buses.length > 0 ? (
        <div style={{ display: "flex", flexDirection: "column" }}>
          {health.buses.map((bus) => (
            <BusRow key={bus.name} bus={bus} />
          ))}
        </div>
      ) : (
        <div style={{ padding: "0.25rem", opacity: 0.6 }}>バス情報なし</div>
      )}
    </div>
  );
}

function CardMode({ health }: { health: HealthSnapshot }) {
  const tone = busTone(health.overall);
  return (
    <fieldset className="tui-fieldset">
      <legend>CAN Health</legend>
      <div
        style={{
          display: "flex",
          flexDirection: "column",
          gap: "0.75rem",
          padding: "0.25rem",
        }}
      >
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
          }}
        >
          <span>STATUS</span>
          <StatusTag tone={tone} />
        </div>
        {health.buses.length > 0 ? (
          <div style={{ display: "flex", flexDirection: "column", gap: "0.25rem" }}>
            <span style={{ opacity: 0.7 }}>バス ({health.buses.length})</span>
            {health.buses.map((bus) => (
              <BusRow key={bus.name} bus={bus} />
            ))}
          </div>
        ) : null}
      </div>
    </fieldset>
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
      <div style={{ display: "flex", justifyContent: "flex-end" }}>
        <span style={{ opacity: 0.6 }}>未取得</span>
      </div>
    );
  }
  return (
    <fieldset className="tui-fieldset">
      <legend>CAN Health</legend>
      <p style={{ padding: "0.25rem", opacity: 0.7 }}>ヘルス情報未取得</p>
    </fieldset>
  );
}

export function HealthIndicator({ health, variant = "compact" }: HealthIndicatorProps) {
  if (!health) return <NeutralPlaceholder variant={variant} />;
  if (variant === "pill") return <PillMode health={health} />;
  if (variant === "compact") return <CompactMode health={health} />;
  if (variant === "bus-only") return <BusOnlyMode health={health} />;
  return <CardMode health={health} />;
}
