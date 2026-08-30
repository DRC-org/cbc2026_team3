import { formatAge } from "@/components/diagnostics/HealthIndicator";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { cx } from "@/lib/cx";
import { motorTempTone } from "@/lib/healthVerdict";
import type { TempThresholds } from "@/lib/healthVerdict";
import type { MotorHealth, MotorHealthState, MotorState } from "@/lib/protocol";
import type { Tone } from "@/lib/tone";
import { TONE_TEXT_CLASS } from "@/lib/tone";

interface MotorStatusProps {
  name: string;
  state: MotorState;
  health?: MotorHealth;
  /** 温度の色分けに使うしきい値。サーバー由来で、MotorSummary から流れてくる */
  tempThresholds?: TempThresholds | null;
  className?: string;
}

const HEALTH_TONE: Record<MotorHealthState, Tone> = {
  ok: "success",
  stale: "warning",
  warning: "warning",
  fault: "error",
};

/**
 * 温度帯に応じた文字色。正常域は地の文字色のままにする
 * (全モータが緑に光ると、本当に見るべき 1 基が沈む)。
 * しきい値が未取得なら `motorTempTone` が neutral を返すので、色は付かない。
 */
function tempTextClass(temp: number, thresholds: TempThresholds | null): string {
  const tone = motorTempTone(temp, thresholds);
  return tone === "warning" || tone === "error" ? cx(TONE_TEXT_CLASS[tone], "font-medium") : "";
}

/** 4 値の桁位置をモータ間で揃えるためのグリッド。ヘッダーと値行で共有する */
const STAT_GRID_CLASS = "grid grid-cols-4 gap-1 px-1 text-right";

const STAT_LABELS = ["POS", "VEL", "TRQ", "TMP"];

/**
 * 数値列の見出し。モータ 1 基ごとに `POS` `VEL` などを併記すると、
 * 幅 300px の診断カラムでは値の桁数しだいでラベルが `OS` まで削られて読めなくなる。
 * 見出しは一覧に 1 行だけ置き、各行は数値だけを並べる。
 */
export function MotorStatHeader({ className }: { className?: string }) {
  return (
    <div className={cx(STAT_GRID_CLASS, "text-[0.8em] text-base-content/60", className)}>
      {STAT_LABELS.map((label) => (
        <span key={label}>{label}</span>
      ))}
    </div>
  );
}

function Cell({ value, unit, toneClass }: { value: string; unit?: string; toneClass?: string }) {
  return (
    <span className={cx("truncate font-mono tabular-nums", toneClass)}>
      {value}
      {unit ? <span className="text-base-content/60">{unit}</span> : null}
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
  // モータ名と数値を同じ行に並べると、サイドカラム幅ではモータ名が "li..." まで
  // 削られて識別できなくなる。名前を独立した行に出して常に読めるようにする
  return (
    <div className={cx("flex flex-col py-[0.15rem]", className)}>
      <div className="flex min-w-0 items-center justify-between gap-2 px-1">
        <span className="min-w-0 truncate font-medium">{name}</span>
        {health ? (
          <span className="flex shrink-0 items-center gap-1.5 whitespace-nowrap">
            <StatusBadge tone={HEALTH_TONE[health.state]}>{health.state.toUpperCase()}</StatusBadge>
            <span className="text-[0.8em] text-base-content/60">
              {formatAge(health.feedback_age_ms)}
            </span>
          </span>
        ) : null}
      </div>
      {/* 見出しは MotorStatHeader が一覧に 1 行だけ出す。同じグリッドを使って桁位置を揃える */}
      <div className={STAT_GRID_CLASS}>
        <Cell value={state.pos.toFixed(1)} />
        <Cell value={state.vel.toFixed(1)} />
        <Cell value={state.torque.toFixed(1)} />
        <Cell
          value={state.temp.toFixed(1)}
          unit="℃"
          toneClass={tempTextClass(state.temp, tempThresholds)}
        />
      </div>
      {/*
        ドライバからの補足。平常時は null なので 1px も占めない。
        状態バッジ (WARNING) が目を引く役、この行が「何をすればよいか」を伝える役で、
        色は付けない —— 状態はチップが示す約束なので、着色テキストを重ねない
      */}
      {health?.detail ? (
        <p className="px-1 pt-0.5 text-[0.8em] leading-snug text-base-content/80">
          {health.detail}
        </p>
      ) : null}
    </div>
  );
}
