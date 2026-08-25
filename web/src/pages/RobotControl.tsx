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
import { Button } from "@/components/ui/Button";
import { Panel } from "@/components/ui/Panel";
import { useRobot } from "@/context/RobotContext";
import { useHotkeys } from "@/hooks/useHotkeys";
import type { ChecklistRole, RobotState } from "@/hooks/useRobotSocket";
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
    <Panel legend="DIAGNOSTICS">
      <div className="group">
        <div className="group-title">CAN BUS</div>
        <HealthIndicator variant="bus-only" health={state.health} />
      </div>

      <div className="group min-h-0 flex-1">
        <div className="group-title">MOTORS</div>
        <MotorSummary motors={state.motors} healthMotors={state.health?.motors} />
      </div>

      <div className="group">
        <div className="hstack flex-wrap">
          <MotorCheckButton robotName={robotKey} onPanelOpen={onPanelOpen} />
          <Button onClick={onPanelOpen}>▤ 結果を表示</Button>
        </div>
      </div>
    </Panel>
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

  // Space に主操作を集約する。ルーターは表示中のタブしか描画しないので、
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
      <main className="page items-center justify-center">
        <Panel legend={label} className="flex-none">
          <p className="text-fg-dim">データ未受信 — 接続待機中...</p>
        </Panel>
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
        <main className="page grid grid-cols-[minmax(0,1fr)_minmax(300px,24rem)]">
          <div className="flex min-h-0 flex-col gap-2">
            {ownsChecklist ? (
              // 指差喚呼は項目数ぶんの高さがあれば足りる。残りはステップ一覧に回すが、
              // 項目が増えても画面の半分までに抑えて一覧を潰さない
              <div className="flex max-h-[50%] min-h-0 shrink-0">
                <Checklist
                  checklistRole={robotKey as ChecklistRole}
                  title={`${label} セッティング指差喚呼`}
                />
              </div>
            ) : (
              <Panel legend="全自動モード" className="shrink-0">
                <p className="text-fg-dim">
                  指差喚呼は Monitor タブ <span className="key-hint">1</span> で実施します。
                </p>
              </Panel>
            )}

            <Panel legend="SEQUENCE PREVIEW" className="flex-1">
              <p className="shrink-0 text-fg-dim">
                {state.sequence} — 全 {state.total_steps} ステップ (試合開始後に操作できます)
              </p>
              <SequenceStepList
                steps={state.steps ?? []}
                stepIndex={state.step_index}
                waitingTrigger={state.waiting_trigger}
                onJump={handleJump}
                disabled
              />
            </Panel>
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
      <main className="page grid grid-cols-[minmax(0,1fr)_minmax(280px,340px)_minmax(280px,340px)]">
        <div className="flex min-h-0 flex-col gap-2">
          <Panel legend="SEQUENCE" className="shrink-0">
            <SequenceProgress
              sequence={state.sequence}
              currentStep={state.current_step}
              stepIndex={state.step_index}
              totalSteps={state.total_steps}
              waitingTrigger={state.waiting_trigger}
            />
          </Panel>

          <CurrentStepPanel
            steps={state.steps ?? []}
            stepIndex={state.step_index}
            totalSteps={state.total_steps}
            waitingTrigger={state.waiting_trigger}
          />

          {/* 開始/停止 + TriggerButton。180px 固定 + 残りで横並び。 */}
          <div className="grid min-h-[88px] shrink-0 grid-cols-[180px_1fr] gap-2">
            {showStop ? (
              // 通常停止は安全側の動作。確認ダイアログを挟むと「止めたいのに止まらない」
              // 時間が生まれるため、ここは 1 アクションで即座に止める
              <Button
                tone="danger"
                onClick={handleStop}
                aria-label="シーケンスを通常停止"
                className="h-full w-full"
              >
                ■ STOP
              </Button>
            ) : (
              <Button
                tone={inMatch ? "ok" : "default"}
                disabled={!inMatch}
                onClick={handleStart}
                aria-label="シーケンスを先頭から開始"
                className="h-full w-full"
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

        <Panel legend="STEPS">
          <SequenceStepList
            steps={state.steps ?? []}
            stepIndex={state.step_index}
            waitingTrigger={state.waiting_trigger}
            onJump={handleJump}
            disabled={!inMatch}
          />
        </Panel>

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
