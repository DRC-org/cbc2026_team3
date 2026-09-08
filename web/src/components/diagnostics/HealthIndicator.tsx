import { StatusBadge } from "@/components/ui/StatusBadge";
import type { BusHealth, BusHealthState, HealthSnapshot } from "@/lib/protocol";
import type { Tone } from "@/lib/tone";

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

const CELL_CLASS = "text-[0.85em]";

function StatusTag({ tone }: { tone: HealthTone }) {
  return <StatusBadge tone={tone}>{TONE_LABEL[tone]}</StatusBadge>;
}

function BusRow({ bus }: { bus: BusHealth }) {
  const tone = busTone(bus.state);
  const notes = [
    bus.bus_off ? "bus_off" : null,
    bus.rx_down ? "rx_down" : null,
    bus.tx_error_count > 0 ? `tx_err ${bus.tx_error_count}` : null,
    bus.rx_error_count > 0 ? `rx_err ${bus.rx_error_count}` : null,
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

export function HealthIndicator({ health }: { health: HealthSnapshot | undefined }) {
  if (!health) {
    return (
      <div className="flex items-center justify-between gap-2">
        <span className="text-base-content/70">CAN</span>
        <StatusTag tone="neutral" />
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-center justify-between gap-2">
        <span className="text-base-content/70">{health.buses.length} 系統</span>
        <StatusTag tone={busTone(health.overall)} />
      </div>
      {health.buses.length === 0 ? (
        <div className="text-base-content/70">バス情報なし</div>
      ) : (
        <div className="overflow-x-auto">
          <table className="table table-zebra table-xs">
            <tbody>
              {health.buses.map((bus) => (
                <BusRow key={bus.name} bus={bus} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
