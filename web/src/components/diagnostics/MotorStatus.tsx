import { StatusBadge } from "@/components/ui/StatusBadge";
import { commandValueText } from "@/lib/commandValue";
import { cx } from "@/lib/cx";
import { motorTempTone } from "@/lib/healthVerdict";
import type { TempThresholds } from "@/lib/healthVerdict";
import { MALFORMED, readCommand, readMeasured } from "@/lib/protocol";
import type {
  Malformed,
  Measured,
  MotorHealth,
  MotorHealthState,
  MotorState,
} from "@/lib/protocol";
import { formatAge } from "@/lib/time";
import type { Tone } from "@/lib/tone";
import { TONE_TEXT_CLASS } from "@/lib/tone";

interface MotorStatusProps {
  name: string;
  state: MotorState;
  health?: MotorHealth;
  tempThresholds?: TempThresholds | null;
  className?: string;
}

const HEALTH_TONE: Record<MotorHealthState, Tone> = {
  ok: "success",
  stale: "warning",
  warning: "warning",
  fault: "error",
};

function tempTextClass(temp: Measured, thresholds: TempThresholds | null): string {
  const tone = motorTempTone(temp, thresholds);
  return tone === "warning" || tone === "error" ? cx(TONE_TEXT_CLASS[tone], "font-medium") : "";
}

const UNMEASURED = "—";

const UNREADABLE = "?";

const COMMAND_MARK = "→";

const COMMAND_TITLE =
  "PC が最後に送った指令値です（実際の出力ではありません）。この基板は出力を測る手段を持たないため、緊急停止・ウォッチドッグ満了・ファーム側の上限クランプで基板が出していなくても、ここには値が残ります。";

const commandDigits = (mode: string | null) => (mode === "duty" ? 2 : 1);

const STAT_GRID_CLASS = "grid grid-cols-4 gap-1 px-1 text-right";

const NAME_COL_CLASS = "@min-[32rem]:w-[11rem]";

const STAT_LABELS = ["POS", "VEL", "TRQ", "TMP"];

export function MotorStatHeader({ className }: { className?: string }) {
  return (
    <div className={cx("flex text-[0.8em] text-base-content/60", className)}>
      <span className={cx("hidden shrink-0 @min-[32rem]:block", NAME_COL_CLASS)} aria-hidden />
      <div className={cx(STAT_GRID_CLASS, "min-w-0 flex-1")}>
        {STAT_LABELS.map((label) => (
          <span key={label}>{label}</span>
        ))}
      </div>
    </div>
  );
}

function Cell({
  value,
  unit,
  toneClass,
}: {
  value: Measured | Malformed;
  unit?: string;
  toneClass?: string;
}) {
  if (value === MALFORMED) {
    return (
      <span className={cx("truncate font-mono tabular-nums", TONE_TEXT_CLASS.error)}>
        {UNREADABLE}
      </span>
    );
  }
  if (value === null) {
    return (
      <span className="truncate font-mono text-base-content/50 tabular-nums">{UNMEASURED}</span>
    );
  }
  return (
    <span className={cx("truncate font-mono tabular-nums", toneClass)}>
      {value.toFixed(1)}
      {unit ? <span className="text-base-content/60">{unit}</span> : null}
    </span>
  );
}

function PositionCell({ state }: { state: MotorState }) {
  const measured = readMeasured(state.pos);
  if (measured !== null) return <Cell value={measured} />;

  const commanded = readCommand(state.command);
  if (commanded === null || commanded === MALFORMED) return <Cell value={commanded} />;

  const mode = typeof state.command_mode === "string" ? state.command_mode : null;
  return (
    <span className="truncate font-mono tabular-nums" title={COMMAND_TITLE}>
      <span className="text-base-content/50">{COMMAND_MARK}</span>
      {commandValueText(commanded, mode, commandDigits(mode))}
    </span>
  );
}

export function MotorStatus({
  name,
  state,
  health,
  tempThresholds = null,
  className,
}: MotorStatusProps) {
  const temp = readMeasured(state.temp);

  return (
    <div
      className={cx(
        "flex flex-col py-[0.15rem] @min-[32rem]:flex-row @min-[32rem]:items-center",
        className,
      )}
    >
      <div
        className={cx(
          "flex min-w-0 items-center justify-between gap-2 px-1 @min-[32rem]:shrink-0 @min-[32rem]:justify-start",
          NAME_COL_CLASS,
        )}
      >
        <span className="min-w-0 truncate font-medium">{name}</span>
        {health ? (
          <span className="flex shrink-0 items-center gap-1.5 whitespace-nowrap">
            <StatusBadge tone={HEALTH_TONE[health.state]} title={health.state.toUpperCase()}>
              {health.state === "ok" ? null : health.state.toUpperCase()}
            </StatusBadge>
            {health.state === "ok" ? null : (
              <span className="text-[0.8em] text-base-content/60">
                {formatAge(health.feedback_age_ms)}
              </span>
            )}
          </span>
        ) : null}
      </div>
      <div className={cx(STAT_GRID_CLASS, "min-w-0 @min-[32rem]:flex-1")}>
        <PositionCell state={state} />
        <Cell value={readMeasured(state.vel)} />
        <Cell value={readMeasured(state.torque)} />
        <Cell
          value={temp}
          unit="℃"
          toneClass={tempTextClass(temp === MALFORMED ? null : temp, tempThresholds)}
        />
      </div>
      {health?.detail ? (
        <p className="px-1 pt-0.5 text-[0.8em] leading-snug text-base-content/80">
          {health.detail}
        </p>
      ) : null}
    </div>
  );
}
