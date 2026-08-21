import { formatAge } from "@/components/HealthIndicator";
import type { MotorHealth, MotorHealthState, MotorState } from "@/hooks/useRobotSocket";
import { TEMP_DANGER, TEMP_WARNING } from "@/lib/robots";

interface MotorStatusProps {
  name: string;
  state: MotorState;
  health?: MotorHealth;
}

type HealthTone = "success" | "warning" | "danger";

const HEALTH_TONE: Record<MotorHealthState, HealthTone> = {
  ok: "success",
  stale: "warning",
  warning: "warning",
  fault: "danger",
};

const HEALTH_TEXT_CLASS: Record<HealthTone, string> = {
  success: "success-text",
  warning: "warning-text",
  danger: "danger-text",
};

type StatTone = "default" | "warning" | "danger";

// 温度帯に応じた文字色。default は地の文字色のまま。
const STAT_TONE_TEXT: Record<StatTone, string> = {
  default: "",
  warning: "warning-text",
  danger: "danger-text",
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
    <div className="motor-cell">
      <span className="dim">{label}</span>
      <span className={`tabular-nums ${STAT_TONE_TEXT[tone]}`}>
        {value}
        {unit ? <span className="dim">{unit}</span> : null}
      </span>
    </div>
  );
}

export function MotorStatus({ name, state, health }: MotorStatusProps) {
  // モータ名と数値を同じ行に並べると、サイドカラム幅ではモータ名が "li..." まで
  // 削られて識別できなくなる。名前を独立した行に出して常に読めるようにする
  return (
    <div className="motor-row">
      <div className="hsplit">
        <span className="ellipsis">{name}</span>
        {health ? (
          <span className="hstack nowrap" style={{ flexShrink: 0 }}>
            <span className={HEALTH_TEXT_CLASS[HEALTH_TONE[health.state]]}>
              {health.state.toUpperCase()}
            </span>
            <span className="dim">{formatAge(health.feedback_age_ms)}</span>
          </span>
        ) : null}
      </div>
      {/* 4 値を等幅グリッドに固定し、モータ間で桁位置が揃うようにする */}
      <div className="motor-cells">
        <Cell label="POS" value={state.pos.toFixed(1)} />
        <Cell label="VEL" value={state.vel.toFixed(1)} />
        <Cell label="TRQ" value={state.torque.toFixed(1)} />
        <Cell label="TMP" value={state.temp.toFixed(1)} unit="℃" tone={tempTone(state.temp)} />
      </div>
    </div>
  );
}
