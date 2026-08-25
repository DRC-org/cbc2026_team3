import { formatAge } from "@/components/HealthIndicator";
import type { MotorHealth, MotorHealthState, MotorState } from "@/hooks/useRobotSocket";
import { cx } from "@/lib/cx";
import { TEMP_DANGER, TEMP_WARNING } from "@/lib/robots";
import type { Tone } from "@/lib/tone";
import { TONE_TEXT_CLASS } from "@/lib/tone";

interface MotorStatusProps {
  name: string;
  state: MotorState;
  health?: MotorHealth;
}

const HEALTH_TONE: Record<MotorHealthState, Tone> = {
  ok: "success",
  stale: "warning",
  warning: "warning",
  fault: "error",
};

type StatTone = "default" | "warning" | "danger";

// 温度帯に応じた文字色。default は地の文字色のまま。
const STAT_TONE_TEXT: Record<StatTone, string> = {
  default: "",
  warning: "text-warning",
  danger: "text-error",
};

function tempTone(temp: number): StatTone {
  if (temp >= TEMP_DANGER) return "danger";
  if (temp >= TEMP_WARNING) return "warning";
  return "default";
}

interface CellProps {
  label: string;
  value: string;
  unit?: string;
  tone?: StatTone;
}

function Cell({ label, value, unit, tone = "default" }: CellProps) {
  return (
    <div className="flex min-w-0 items-baseline justify-end gap-[0.3em]">
      <span className="text-fg-dim">{label}</span>
      <span className={cx("tabular-nums", STAT_TONE_TEXT[tone])}>
        {value}
        {unit ? <span className="text-fg-dim">{unit}</span> : null}
      </span>
    </div>
  );
}

export function MotorStatus({ name, state, health }: MotorStatusProps) {
  // モータ名と数値を同じ行に並べると、サイドカラム幅ではモータ名が "li..." まで
  // 削られて識別できなくなる。名前を独立した行に出して常に読めるようにする
  return (
    <div className="flex flex-col px-[0.3rem] py-[0.15rem]">
      <div className="hsplit">
        <span className="truncate">{name}</span>
        {health ? (
          <span className="hstack shrink-0 whitespace-nowrap">
            <span className={TONE_TEXT_CLASS[HEALTH_TONE[health.state]]}>
              {health.state.toUpperCase()}
            </span>
            <span className="text-fg-dim">{formatAge(health.feedback_age_ms)}</span>
          </span>
        ) : null}
      </div>
      {/* 4 値を等幅グリッドに固定し、モータ間で桁位置が揃うようにする */}
      <div className="grid grid-cols-4 gap-1">
        <Cell label="POS" value={state.pos.toFixed(1)} />
        <Cell label="VEL" value={state.vel.toFixed(1)} />
        <Cell label="TRQ" value={state.torque.toFixed(1)} />
        <Cell label="TMP" value={state.temp.toFixed(1)} unit="℃" tone={tempTone(state.temp)} />
      </div>
    </div>
  );
}
