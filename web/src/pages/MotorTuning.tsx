import { useState } from "react";

import { MotorStatus } from "@/components/MotorStatus";
import { Button } from "@/components/ui/Button";
import { Panel } from "@/components/ui/Panel";
import { useRobot } from "@/context/RobotContext";
import { ROBOTS } from "@/lib/robots";

const PID_PARAMS = [
  { key: "kp", label: "Kp", max: 10 },
  { key: "ki", label: "Ki", max: 5 },
  { key: "kd", label: "Kd", max: 5 },
] as const;

const STEP = 0.01;

interface PidRowProps {
  label: string;
  max: number;
  value: number;
  onChange: (val: number) => void;
  onSend: () => void;
}

// PID 1 項目の行: ◄ 微減 / レンジ / ► 微増 / 数値表示 / SEND。
// 送信は明示ボタンのみ（スライダー操作だけでは set_param を飛ばさない）。
function PidRow({ label, max, value, onChange, onSend }: PidRowProps) {
  // クランプ後に STEP 単位の浮動小数誤差を丸める。
  const clamp = (val: number) => {
    const next = Math.min(max, Math.max(0, val));
    return Math.round(next / STEP) * STEP;
  };

  return (
    <div className="hstack">
      <span className="w-7 shrink-0 text-fg-dim">{label}</span>
      <Button aria-label={`${label} を減らす`} onClick={() => onChange(clamp(value - STEP))}>
        ◄
      </Button>
      <input
        type="range"
        className="range min-w-0 flex-1 text-info range-xs"
        aria-label={label}
        min={0}
        max={max}
        step={STEP}
        value={value}
        onChange={(e) => onChange(clamp(Number(e.target.value)))}
      />
      <Button aria-label={`${label} を増やす`} onClick={() => onChange(clamp(value + STEP))}>
        ►
      </Button>
      <span className="w-14 shrink-0 text-right tabular-nums">{value.toFixed(2)}</span>
      <Button tone="info" aria-label={`${label} を送信`} onClick={onSend}>
        ► SEND
      </Button>
    </div>
  );
}

export function MotorTuning() {
  const { states, send } = useRobot();
  const [values, setValues] = useState<Record<string, Record<string, number>>>({});

  const getValue = (motor: string, param: string) => values[motor]?.[param] ?? 0;

  const setValue = (motor: string, param: string, val: number) => {
    setValues((prev) => ({
      ...prev,
      [motor]: { ...prev[motor], [param]: val },
    }));
  };

  const handleSend = (motor: string, param: string) => {
    send({
      type: "set_param",
      motor,
      key: param,
      value: getValue(motor, param),
    });
  };

  return (
    <main className="page grid grid-cols-2 gap-2">
      {ROBOTS.map(({ key, label }) => {
        const state = states[key];
        const motors = state ? Object.entries(state.motors) : [];
        return (
          <Panel key={key} legend={label}>
            {!state ? (
              <p className="text-fg-dim">データ未受信 — 接続待機中...</p>
            ) : motors.length === 0 ? (
              <p className="text-fg-dim">モータ情報なし</p>
            ) : (
              // モータ数が増えても枠内のみスクロールさせ全体スクロールは禁止する。
              // モータごとの枠は入れ子にせず、罫線 1 本で区切る
              <div className="panel-body scroll">
                {motors.map(([motorName, motorState]) => (
                  <div key={motorName} className="group">
                    {/* 見出しは MotorStatus 側のモータ名行が兼ねる */}
                    <MotorStatus name={motorName} state={motorState} />
                    {PID_PARAMS.map(({ key: paramKey, label: paramLabel, max }) => (
                      <PidRow
                        key={paramKey}
                        label={paramLabel}
                        max={max}
                        value={getValue(motorName, paramKey)}
                        onChange={(val) => setValue(motorName, paramKey, val)}
                        onSend={() => handleSend(motorName, paramKey)}
                      />
                    ))}
                  </div>
                ))}
              </div>
            )}
          </Panel>
        );
      })}
    </main>
  );
}
