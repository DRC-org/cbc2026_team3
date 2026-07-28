import { Button } from "@tsaito18/tuicss-react";
import { useState } from "react";

import { HealthIndicator } from "@/components/HealthIndicator";
import { Modal } from "@/components/Modal";
import { MotorCheckButton } from "@/components/MotorCheckButton";
import { MotorCheckPanel } from "@/components/MotorCheckPanel";
import { MotorSummary } from "@/components/MotorSummary";
import { SequenceProgress } from "@/components/SequenceProgress";
import { SequenceStepList } from "@/components/SequenceStepList";
import { TriggerButton } from "@/components/TriggerButton";
import { useRobot } from "@/context/RobotContext";

interface RobotControlProps {
  robotKey: string;
  label: string;
}

export function RobotControl({ robotKey, label }: RobotControlProps) {
  const { states, send } = useRobot();
  const state = states[robotKey];
  const [stopConfirmOpen, setStopConfirmOpen] = useState(false);
  const [healthCheckOpen, setHealthCheckOpen] = useState(false);

  const handleTrigger = () => {
    send({ type: "trigger", robot: robotKey });
  };

  const handleJump = (stepIndex: number) => {
    send({ type: "sequence_jump", robot: robotKey, step_index: stepIndex });
  };

  const handleConfirmStop = () => {
    send({ type: "sequence_stop", robot: robotKey });
    setStopConfirmOpen(false);
  };

  const handleStart = () => {
    send({ type: "sequence_start", robot: robotKey });
  };

  const completed = state && state.total_steps > 0 && state.step_index >= state.total_steps;
  const idleStopped =
    state &&
    state.total_steps > 0 &&
    !state.waiting_trigger &&
    state.step_index === 0 &&
    !completed;
  const inProgress = state && !state.waiting_trigger && !completed && !idleStopped;
  const showStop = Boolean(inProgress || state?.waiting_trigger);

  if (!state) {
    return (
      <main
        style={{
          display: "flex",
          flex: 1,
          alignItems: "center",
          justifyContent: "center",
          overflow: "hidden",
          padding: "1.5rem",
          minHeight: 0,
        }}
      >
        <div className="tui-window">
          <fieldset className="tui-fieldset">
            <legend>{label}</legend>
            <p style={{ padding: "1rem 0.5rem", opacity: 0.8 }}>データ未受信 — 接続待機中...</p>
          </fieldset>
        </div>
      </main>
    );
  }

  return (
    <main
      style={{
        display: "grid",
        gridTemplateColumns: "minmax(0,1fr) minmax(280px,340px) minmax(280px,340px)",
        gap: "0.75rem",
        overflow: "hidden",
        padding: "0.75rem",
        minHeight: 0,
        flex: 1,
      }}
    >
      {/* 左カラム: シーケンス概観 + コントロールバー */}
      <div
        style={{
          display: "flex",
          flexDirection: "column",
          gap: "0.75rem",
          overflow: "hidden",
        }}
      >
        <div className="tui-window" style={{ flexShrink: 0 }}>
          <fieldset className="tui-fieldset">
            <legend>SEQUENCE</legend>
            <SequenceProgress
              sequence={state.sequence}
              currentStep={state.current_step}
              stepIndex={state.step_index}
              totalSteps={state.total_steps}
              waitingTrigger={state.waiting_trigger}
            />
          </fieldset>
        </div>

        <div style={{ flex: 1 }} aria-hidden="true" />

        {/* 開始/停止 + TriggerButton。180px 固定 + 残りで横並び。 */}
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "180px 1fr",
            gap: "0.75rem",
            flexShrink: 0,
            minHeight: 88,
          }}
        >
          {showStop ? (
            <Button
              className="red-255"
              onClick={() => setStopConfirmOpen(true)}
              aria-label="シーケンスを通常停止"
              style={{ width: "100%" }}
            >
              ■ STOP
            </Button>
          ) : (
            <Button
              className="green-255"
              onClick={handleStart}
              aria-label="シーケンスを先頭から開始"
              style={{ width: "100%" }}
            >
              ► START
            </Button>
          )}
          <TriggerButton
            waiting={state.waiting_trigger}
            stepIndex={state.step_index}
            totalSteps={state.total_steps}
            onTrigger={handleTrigger}
          />
        </div>
      </div>

      {/* 中カラム: ステップ一覧 (縦スタック) */}
      <div
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
          <legend>STEPS</legend>
          <SequenceStepList
            steps={state.steps ?? []}
            stepIndex={state.step_index}
            waitingTrigger={state.waiting_trigger}
            onJump={handleJump}
          />
        </fieldset>
      </div>

      {/* 右カラム: CAN Bus + モータ */}
      <div
        style={{
          display: "flex",
          flexDirection: "column",
          gap: "0.75rem",
          overflow: "hidden",
        }}
      >
        <div className="tui-window" style={{ flexShrink: 0 }}>
          <fieldset className="tui-fieldset">
            <legend>CAN BUS</legend>
            <HealthIndicator variant="bus-only" health={state.health} />
          </fieldset>
        </div>
        <div
          className="tui-window"
          style={{
            display: "flex",
            flexDirection: "column",
            flex: 1,
            minHeight: 0,
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
            <legend>MOTORS</legend>
            <MotorSummary motors={state.motors} />
          </fieldset>
        </div>
      </div>

      <div
        style={{
          display: "flex",
          flexShrink: 0,
          flexWrap: "wrap",
          alignItems: "center",
          gap: 8,
        }}
      >
        <MotorCheckButton robotName={robotKey} onPanelOpen={() => setHealthCheckOpen(true)} />
        <Button className="yellow-255" onClick={() => setHealthCheckOpen(true)}>
          ▤ 結果を表示
        </Button>
      </div>

      <MotorCheckPanel
        robotName={robotKey}
        isOpen={healthCheckOpen}
        onOpenChange={(open) => setHealthCheckOpen(open)}
      />

      <Modal
        isOpen={stopConfirmOpen}
        title="STOP SEQUENCE"
        footer={
          <div
            style={{
              display: "flex",
              justifyContent: "flex-end",
              gap: "0.5rem",
            }}
          >
            <Button onClick={() => setStopConfirmOpen(false)}>キャンセル</Button>
            <Button className="red-255" onClick={handleConfirmStop}>
              停止
            </Button>
          </div>
        }
      >
        <p>シーケンスを停止しますか？</p>
        <p style={{ marginTop: "0.5rem" }}>⚠ 緊急停止 (EMG STOP) ではなく、通常停止です。</p>
        <p style={{ opacity: 0.8 }}>停止後はステップ #1 に戻り、待機状態になります。</p>
      </Modal>
    </main>
  );
}
