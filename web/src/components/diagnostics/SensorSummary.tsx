import { StatusBadge } from "@/components/ui/StatusBadge";
import { MALFORMED } from "@/lib/protocol";
import type { Malformed, SensorState } from "@/lib/protocol";
import type { Tone } from "@/lib/tone";

export type SensorPayload = Record<string, SensorState> | Malformed;

interface SensorSummaryProps {
  sensors: SensorPayload | undefined;
}

interface SensorVerdict {
  tone: Tone;
  /** null はドットの色だけで伝える（平常時の文字を出さない） */
  label: string | null;
  title: string;
}

function verdictOf(sensor: SensorState): SensorVerdict {
  if (sensor.stale) return { tone: "warning", label: "STALE", title: "STALE" };
  if (sensor.active === null) return { tone: "neutral", label: "—", title: "測定手段なし" };
  return sensor.active
    ? { tone: "info", label: "接触", title: "接触" }
    : { tone: "neutral", label: null, title: "開放" };
}

export function SensorSummary({ sensors }: SensorSummaryProps) {
  if (sensors === undefined) return null;

  if (sensors === MALFORMED) {
    return (
      <div className="flex shrink-0 flex-col gap-1 border-l-[0.25rem] border-l-error bg-error/5 px-2 py-1">
        <span className="font-medium">センサ 判定不能</span>
        <span className="text-[0.85em] text-base-content/70">
          センサの配信を読めていません。原点スイッチが反応しているかを画面から確かめられない状態です
        </span>
      </div>
    );
  }

  const entries = Object.entries(sensors);
  if (entries.length === 0) return null;

  return (
    <div className="flex shrink-0 flex-col gap-1">
      <div className="flex flex-col [&>*:nth-child(odd)]:bg-base-200">
        {entries.map(([name, sensor]) => {
          const verdict = verdictOf(sensor);
          return (
            <div
              key={name}
              className="flex min-w-0 items-center justify-between gap-2 px-1 py-[0.15rem]"
            >
              <span className="min-w-0 truncate font-medium">{name}</span>
              <StatusBadge tone={verdict.tone} title={verdict.title}>
                {verdict.label}
              </StatusBadge>
            </div>
          );
        })}
      </div>
    </div>
  );
}
