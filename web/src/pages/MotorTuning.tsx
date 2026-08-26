import { Send } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { Page } from "@/components/ui/Page";
import { Panel } from "@/components/ui/Panel";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { useRobot } from "@/context/RobotContext";
import type { MotorState } from "@/hooks/useRobotSocket";
import { cx } from "@/lib/cx";
import { ROBOTS, TEMP_DANGER, TEMP_WARNING } from "@/lib/robots";

const PID_PARAMS = [
  { key: "kp", label: "Kp", max: 10 },
  { key: "ki", label: "Ki", max: 5 },
  { key: "kd", label: "Kd", max: 5 },
] as const;

const STEP = 0.01;

interface Selection {
  robot: string;
  motor: string;
}

function tempTone(temp: number) {
  if (temp >= TEMP_DANGER) return "error" as const;
  if (temp >= TEMP_WARNING) return "warning" as const;
  return "success" as const;
}

/** 調整対象の 1 値を大きく出す。応答を見ながら詰める作業なので視認性を優先する */
function Readout({ label, value, unit }: { label: string; value: string; unit?: string }) {
  return (
    <div className="flex min-w-0 flex-col items-end">
      <span className="text-[0.8em] text-base-content/60">{label}</span>
      <span className="font-mono text-[1.5em] leading-tight tabular-nums">
        {value}
        {unit ? <span className="text-[0.7em] text-base-content/60">{unit}</span> : null}
      </span>
    </div>
  );
}

interface PidRowProps {
  label: string;
  max: number;
  value: number;
  onChange: (val: number) => void;
}

// PID 1 項目の行: 数値入力 + レンジ。送信は行ごとではなく下の「送信」に集約する。
function PidRow({ label, max, value, onChange }: PidRowProps) {
  // クランプ後に STEP 単位の浮動小数誤差を丸める。
  const clamp = (val: number) => {
    if (!Number.isFinite(val)) return 0;
    const next = Math.min(max, Math.max(0, val));
    return Math.round(next / STEP) * STEP;
  };

  return (
    <div className="flex items-center gap-3">
      <label className="w-8 shrink-0 font-mono text-base-content/70" htmlFor={`pid-${label}`}>
        {label}
      </label>
      {/* スライダーだけだと 0.01 刻みで狙った値に置けない。直接入力を主にする */}
      <input
        id={`pid-${label}`}
        type="number"
        className="input w-24 shrink-0 border-base-300 bg-base-100 text-right font-mono tabular-nums input-sm"
        min={0}
        max={max}
        step={STEP}
        value={value.toFixed(2)}
        onChange={(e) => onChange(clamp(Number(e.target.value)))}
      />
      <input
        type="range"
        className="range min-w-0 flex-1 text-info [--range-thumb:var(--color-info)] range-xs"
        aria-label={`${label} スライダー`}
        min={0}
        max={max}
        step={STEP}
        value={value}
        onChange={(e) => onChange(clamp(Number(e.target.value)))}
      />
      <span className="w-10 shrink-0 text-right text-[0.85em] text-base-content/50 tabular-nums">
        {max}
      </span>
    </div>
  );
}

/**
 * PID 調整タブ。マスタ・ディテール構成。
 *
 * 以前は両機の全モータを縦に展開しており、1 基を触るだけでもスクロールが要り、
 * 「今どのモータを見ているのか」が視界から外れていた。調整は 1 基ずつ応答を見ながら
 * 詰める作業なので、左で対象を選び、右をその 1 基に明け渡す形にする。
 */
export function MotorTuning() {
  const { states, send } = useRobot();
  const [values, setValues] = useState<Record<string, Record<string, number>>>({});
  const [selected, setSelected] = useState<Selection | null>(null);

  const entries = ROBOTS.flatMap(({ key, label }) => {
    const state = states[key];
    return Object.entries(state?.motors ?? {}).map(([motor, motorState]) => ({
      robot: key,
      robotLabel: label,
      motor,
      motorState,
    }));
  });

  // 初回描画時にはまだモータが届いていない。届いた時点で先頭を自動選択する
  const active =
    entries.find((e) => selected && e.robot === selected.robot && e.motor === selected.motor) ??
    entries[0];

  const getValue = (motor: string, param: string) => values[motor]?.[param] ?? 0;

  const setValue = (motor: string, param: string, val: number) => {
    setValues((prev) => ({ ...prev, [motor]: { ...prev[motor], [param]: val } }));
  };

  // 3 項目を個別に送ると PID が中途半端に混ざった状態が一瞬できる。まとめて送る
  const sendAll = (motor: string) => {
    for (const { key } of PID_PARAMS) {
      send({ type: "set_param", motor, key, value: getValue(motor, key) });
    }
  };

  if (entries.length === 0) {
    return (
      <Page className="flex flex-col items-center justify-center">
        <Panel legend="PID TUNING" className="flex-none">
          <p className="text-base-content/70">モータ情報なし — 接続待機中...</p>
        </Panel>
      </Page>
    );
  }

  return (
    <Page className="grid grid-cols-[minmax(13rem,18rem)_minmax(0,1fr)]">
      <Panel legend="モータ" bodyClassName="p-0">
        <div className="scroll flex min-h-0 flex-1 flex-col">
          {ROBOTS.map(({ key, label }) => {
            const group = entries.filter((e) => e.robot === key);
            if (group.length === 0) return null;
            return (
              <div key={key} className="flex flex-col">
                <div className="sticky top-0 border-b border-base-300 bg-base-200 px-2 py-[0.15rem] text-[0.85em] text-base-content/70">
                  {label}
                </div>
                {group.map(({ motor, motorState }) => {
                  const isActive = active?.robot === key && active?.motor === motor;
                  return (
                    <button
                      key={motor}
                      type="button"
                      onClick={() => setSelected({ robot: key, motor })}
                      aria-current={isActive ? "true" : undefined}
                      className={cx(
                        "flex cursor-pointer items-center gap-2 border-l-2 border-transparent px-2 py-[0.3rem] text-left hover:bg-base-200",
                        isActive && "border-l-info bg-base-200 font-medium",
                      )}
                    >
                      <span className="min-w-0 flex-1 truncate">{motor}</span>
                      <span className="shrink-0 font-mono text-[0.85em] text-base-content/60 tabular-nums">
                        {motorState.temp.toFixed(0)}℃
                      </span>
                    </button>
                  );
                })}
              </div>
            );
          })}
        </div>
      </Panel>

      {active ? (
        <MotorDetail
          key={`${active.robot}/${active.motor}`}
          robotLabel={active.robotLabel}
          motor={active.motor}
          motorState={active.motorState}
          getValue={getValue}
          setValue={setValue}
          onSend={() => sendAll(active.motor)}
        />
      ) : null}
    </Page>
  );
}

function MotorDetail({
  robotLabel,
  motor,
  motorState,
  getValue,
  setValue,
  onSend,
}: {
  robotLabel: string;
  motor: string;
  motorState: MotorState;
  getValue: (motor: string, param: string) => number;
  setValue: (motor: string, param: string, val: number) => void;
  onSend: () => void;
}) {
  return (
    <Panel
      legend={`${robotLabel} / ${motor}`}
      actions={
        <StatusBadge tone={tempTone(motorState.temp)}>{motorState.temp.toFixed(1)}℃</StatusBadge>
      }
    >
      {/* 応答を見ながら詰めるので、選択中 1 基の現在値は大きく出す */}
      <div className="flex shrink-0 justify-between gap-4 border-b border-base-300 pb-2">
        <Readout label="POS" value={motorState.pos.toFixed(1)} />
        <Readout label="VEL" value={motorState.vel.toFixed(1)} />
        <Readout label="TORQUE" value={motorState.torque.toFixed(2)} />
        <Readout label="TEMP" value={motorState.temp.toFixed(1)} unit="℃" />
      </div>

      <div className="flex flex-col gap-3 pt-3">
        {PID_PARAMS.map(({ key, label, max }) => (
          <PidRow
            key={key}
            label={label}
            max={max}
            value={getValue(motor, key)}
            onChange={(val) => setValue(motor, key, val)}
          />
        ))}
      </div>

      {/* 送信は明示操作のみ。スライダーを触っただけでは set_param を飛ばさない */}
      <div className="mt-3 flex shrink-0 items-center gap-3">
        <Button tone="info" onClick={onSend} aria-label={`${motor} の PID を送信`}>
          <Icon as={Send} />
          この 3 値を送信
        </Button>
        <span className="text-base-content/60">スライダー操作だけでは送信されません</span>
      </div>
    </Panel>
  );
}
