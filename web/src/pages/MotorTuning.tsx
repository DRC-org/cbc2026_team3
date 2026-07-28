import { Button } from "@tsaito18/tuicss-react";
import { useState } from "react";

import { MotorStatus } from "@/components/MotorStatus";
import { useRobot } from "@/context/RobotContext";

const PID_PARAMS = [
  { key: "kp", label: "Kp", max: 10 },
  { key: "ki", label: "Ki", max: 5 },
  { key: "kd", label: "Kd", max: 5 },
] as const;

const ROBOTS = [
  { key: "main_hand", label: "MAIN HAND" },
  { key: "sub_hand", label: "SUB HAND" },
] as const;

const STEP = 0.01;

interface PidRowProps {
  label: string;
  max: number;
  value: number;
  onChange: (val: number) => void;
  onSend: () => void;
}

// PID 1 項目の行: ◄ 微減 / TUI レンジ / ► 微増 / 数値表示 / SEND。
// 送信は明示ボタンのみ（スライダー操作だけでは set_param を飛ばさない）。
function PidRow({ label, max, value, onChange, onSend }: PidRowProps) {
  // クランプ後に STEP 単位の浮動小数誤差を丸める。
  const clamp = (val: number) => {
    const next = Math.min(max, Math.max(0, val));
    return Math.round(next / STEP) * STEP;
  };

  return (
    <div style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
      <span style={{ width: "1.75rem", flexShrink: 0 }}>{label}</span>
      <Button aria-label={`${label} を減らす`} onClick={() => onChange(clamp(value - STEP))}>
        ◄
      </Button>
      <input
        type="range"
        style={{ flex: 1 }}
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
      <span
        style={{
          width: "3.5rem",
          flexShrink: 0,
          textAlign: "right",
          fontVariantNumeric: "tabular-nums",
        }}
      >
        {value.toFixed(2)}
      </span>
      <Button className="blue-255" aria-label={`${label} を送信`} onClick={onSend}>
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
    <main
      style={{
        display: "grid",
        gridTemplateColumns: "repeat(2,minmax(0,1fr))",
        gap: "0.75rem",
        minHeight: 0,
        flex: 1,
        overflow: "hidden",
        padding: "0.75rem",
      }}
    >
      {ROBOTS.map(({ key, label }) => {
        const state = states[key];
        const motors = state ? Object.entries(state.motors) : [];
        return (
          <div
            key={key}
            className="tui-window"
            style={{
              display: "flex",
              flexDirection: "column",
              flex: 1,
              minHeight: 0,
              height: "100%",
              overflow: "hidden",
            }}
          >
            <fieldset
              className="tui-fieldset"
              style={{
                display: "flex",
                flexDirection: "column",
                flex: 1,
                minHeight: 0,
              }}
            >
              <legend>{label}</legend>

              {!state ? (
                <p style={{ padding: "1rem 0.5rem", opacity: 0.8 }}>データ未受信 — 接続待機中...</p>
              ) : motors.length === 0 ? (
                <p style={{ padding: "1rem 0.5rem", opacity: 0.8 }}>モータ情報なし</p>
              ) : (
                // モータ数が増えても枠内のみスクロールさせ全体スクロールは禁止する。
                <div
                  className="tui-scroll"
                  style={{
                    display: "flex",
                    flexDirection: "column",
                    flex: 1,
                    gap: "0.75rem",
                    paddingRight: "0.25rem",
                  }}
                >
                  {motors.map(([motorName, motorState]) => (
                    <fieldset key={motorName} className="tui-fieldset" style={{ marginBottom: 0 }}>
                      <legend>{motorName}</legend>
                      <div style={{ marginBottom: "0.5rem" }}>
                        <MotorStatus name={motorName} state={motorState} />
                      </div>
                      <div
                        style={{
                          borderTop: "1px solid rgba(255,255,255,0.3)",
                          paddingTop: "0.5rem",
                        }}
                      >
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
                    </fieldset>
                  ))}
                </div>
              )}
            </fieldset>
          </div>
        );
      })}
    </main>
  );
}
