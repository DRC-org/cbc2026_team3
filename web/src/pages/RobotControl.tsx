import { Button, Fieldset } from "@tsaito18/tuicss-react";
import { useState } from "react";

import { Checklist } from "@/components/Checklist";
import { CurrentStepPanel } from "@/components/CurrentStepPanel";
import { HealthIndicator } from "@/components/HealthIndicator";
import { MotorCheckButton } from "@/components/MotorCheckButton";
import { MotorCheckPanel } from "@/components/MotorCheckPanel";
import { MotorSummary } from "@/components/MotorSummary";
import { SequenceProgress } from "@/components/SequenceProgress";
import { SequenceStepList } from "@/components/SequenceStepList";
import { TriggerButton } from "@/components/TriggerButton";
import { useRobot } from "@/context/RobotContext";
import { useHotkeys } from "@/hooks/useHotkeys";
import type { ChecklistRole, RobotState } from "@/hooks/useRobotSocket";
import { cx } from "@/lib/cx";
import { isSetupPhase } from "@/lib/phase";

interface RobotControlProps {
  robotKey: string;
  label: string;
}

/**
 * CAN バス / モータ / 動作確認。準備中も試合中も右カラムに置く共通ブロック。
 *
 * 3 つの見出しは別々のパネルにせず 1 枠にまとめる。診断情報は「どれか 1 つを見る」
 * ものではなく上から順に流し読みする対象なので、枠で分断しないほうが速く読める。
 */
function DiagnosticsColumn({
  robotKey,
  state,
  onPanelOpen,
}: {
  robotKey: string;
  state: RobotState;
  onPanelOpen: () => void;
}) {
  return (
    <Fieldset className="panel" legend="DIAGNOSTICS">
      <div className="group">
        <div className="group-title">CAN BUS</div>
        <HealthIndicator variant="bus-only" health={state.health} />
      </div>

      <div className="group group-fill">
        <div className="group-title">MOTORS</div>
        <MotorSummary motors={state.motors} healthMotors={state.health?.motors} />
      </div>

      <div className="group">
        <div className="hstack" style={{ flexWrap: "wrap" }}>
          <MotorCheckButton robotName={robotKey} onPanelOpen={onPanelOpen} />
          <Button onClick={onPanelOpen}>▤ 結果を表示</Button>
        </div>
      </div>
    </Fieldset>
  );
}

export function RobotControl({ robotKey, label }: RobotControlProps) {
  const { states, send, matchState } = useRobot();
  const state = states[robotKey];
  const [healthCheckOpen, setHealthCheckOpen] = useState(false);

  const handleTrigger = () => send({ type: "trigger", robot: robotKey });
  const handleJump = (stepIndex: number) =>
    send({ type: "sequence_jump", robot: robotKey, step_index: stepIndex });
  const handleStop = () => send({ type: "sequence_stop", robot: robotKey });
  const handleStart = () => send({ type: "sequence_start", robot: robotKey });

  // シーケンス操作が許されるのは試合中のみ (サーバー側のフェーズゲートと対応)
  const inMatch = matchState.phase === "match";
  const setupPhase = isSetupPhase(matchState.phase);
  // 半自動では操縦者が自分のタブで指差喚呼を行う。全自動では Monitor 側に表示される
  const ownsChecklist = matchState.mode === "semi_auto";
  const blockedLabel = matchState.phase === "finished" ? "試合終了" : "準備中";

  const completed = state && state.total_steps > 0 && state.step_index >= state.total_steps;
  const idleStopped =
    state &&
    state.total_steps > 0 &&
    !state.waiting_trigger &&
    state.step_index === 0 &&
    !completed;
  const inProgress = state && !state.waiting_trigger && !completed && !idleStopped;
  const showStop = Boolean(inProgress || state?.waiting_trigger);

  // Space に主操作を集約する。非アクティブなタブの TabPanel は unmount されるので、
  // 表示中のロボットにだけ届く。トリガー待ちなら NEXT、待機中なら START に解決する
  useHotkeys(
    {
      " ": () => {
        if (!inMatch || !state) return;
        if (state.waiting_trigger) handleTrigger();
        else if (!showStop) handleStart();
      },
    },
    inMatch,
  );

  if (!state) {
    return (
      <main className="page" style={{ alignItems: "center", justifyContent: "center" }}>
        <Fieldset className="panel" legend={label} style={{ flex: "0 0 auto" }}>
          <p className="dim">データ未受信 — 接続待機中...</p>
        </Fieldset>
      </main>
    );
  }

  const panel = (
    <MotorCheckPanel
      robotName={robotKey}
      isOpen={healthCheckOpen}
      onOpenChange={setHealthCheckOpen}
    />
  );

  // --- セッティングタイム: 指差喚呼と動作確認が主役。シーケンス操作は出さない ---
  if (setupPhase) {
    return (
      <>
        <main
          className="page"
          style={{
            display: "grid",
            gridTemplateColumns: "minmax(0, 1fr) minmax(300px, 24rem)",
          }}
        >
          <div className="vstack">
            {ownsChecklist ? (
              // 指差喚呼は項目数ぶんの高さがあれば足りる。残りはステップ一覧に回すが、
              // 項目が増えても画面の半分までに抑えて一覧を潰さない
              <div style={{ display: "flex", flexShrink: 0, maxHeight: "50%", minHeight: 0 }}>
                <Checklist
                  checklistRole={robotKey as ChecklistRole}
                  title={`${label} セッティング指差喚呼`}
                />
              </div>
            ) : (
              <Fieldset className="panel no-shrink" legend="全自動モード">
                <p className="dim">
                  指差喚呼は Monitor タブ <span className="key-hint">1</span> で実施します。
                </p>
              </Fieldset>
            )}

            <Fieldset className="panel panel-fill" legend="SEQUENCE PREVIEW">
              <p className="dim no-shrink">
                {state.sequence} — 全 {state.total_steps} ステップ (試合開始後に操作できます)
              </p>
              <SequenceStepList
                steps={state.steps ?? []}
                stepIndex={state.step_index}
                waitingTrigger={state.waiting_trigger}
                onJump={handleJump}
                disabled
              />
            </Fieldset>
          </div>

          <DiagnosticsColumn
            robotKey={robotKey}
            state={state}
            onPanelOpen={() => setHealthCheckOpen(true)}
          />
        </main>
        {panel}
      </>
    );
  }

  // --- 試合中 / 試合終了: シーケンス操作が主役 ---
  return (
    <>
      <main
        className="page"
        style={{
          display: "grid",
          gridTemplateColumns: "minmax(0,1fr) minmax(280px,340px) minmax(280px,340px)",
        }}
      >
        <div className="vstack">
          <Fieldset className="panel no-shrink" legend="SEQUENCE">
            <SequenceProgress
              sequence={state.sequence}
              currentStep={state.current_step}
              stepIndex={state.step_index}
              totalSteps={state.total_steps}
              waitingTrigger={state.waiting_trigger}
            />
          </Fieldset>

          <CurrentStepPanel
            steps={state.steps ?? []}
            stepIndex={state.step_index}
            totalSteps={state.total_steps}
            waitingTrigger={state.waiting_trigger}
          />

          {/* 開始/停止 + TriggerButton。180px 固定 + 残りで横並び。 */}
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "180px 1fr",
              gap: "0.5rem",
              flexShrink: 0,
              minHeight: 88,
            }}
          >
            {showStop ? (
              // 通常停止は安全側の動作。確認ダイアログを挟むと「止めたいのに止まらない」
              // 時間が生まれるため、ここは 1 アクションで即座に止める
              <Button
                className="btn-danger"
                onClick={handleStop}
                aria-label="シーケンスを通常停止"
                style={{ width: "100%" }}
              >
                ■ STOP
              </Button>
            ) : (
              <Button
                className={cx(inMatch && "btn-ok")}
                disabled={!inMatch}
                onClick={handleStart}
                aria-label="シーケンスを先頭から開始"
                style={{ width: "100%" }}
              >
                {inMatch ? "► START" : `⊘ ${blockedLabel}`}
                {inMatch ? <span className="key-hint">Space</span> : null}
              </Button>
            )}
            <TriggerButton
              waiting={state.waiting_trigger}
              stepIndex={state.step_index}
              totalSteps={state.total_steps}
              onTrigger={handleTrigger}
              disabled={!inMatch}
              disabledLabel={blockedLabel}
            />
          </div>
        </div>

        <Fieldset className="panel" legend="STEPS">
          <SequenceStepList
            steps={state.steps ?? []}
            stepIndex={state.step_index}
            waitingTrigger={state.waiting_trigger}
            onJump={handleJump}
            disabled={!inMatch}
          />
        </Fieldset>

        <DiagnosticsColumn
          robotKey={robotKey}
          state={state}
          onPanelOpen={() => setHealthCheckOpen(true)}
        />
      </main>
      {panel}
    </>
  );
}
