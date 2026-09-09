import { TriangleAlert } from "lucide-react";
import { useState } from "react";

import { SubsystemStatus } from "@/components/diagnostics/SubsystemStatus";
import { HomingButtons } from "@/components/homing/HomingButtons";
import { HomingPanel } from "@/components/homing/HomingPanel";
import { ActionPanel } from "@/components/operator/ActionPanel";
import { AlwaysManualPanel } from "@/components/operator/AlwaysManualPanel";
import { ManualPanel } from "@/components/operator/ManualPanel";
import { MatchTimer } from "@/components/operator/MatchTimer";
import { ModeSwitch } from "@/components/operator/ModeSwitch";
import { SequenceStepList } from "@/components/operator/SequenceStepList";
import { SuctionPadPanel } from "@/components/operator/SuctionPadPanel";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { Modal } from "@/components/ui/Modal";
import { Page } from "@/components/ui/Page";
import { Panel } from "@/components/ui/Panel";
import { useRobotCommands, useRobotStates, useRobotStatus } from "@/context/RobotContext";
import { useHotkeys } from "@/hooks/useHotkeys";
import { tempThresholdsOf } from "@/lib/healthVerdict";
import { homingStatus } from "@/lib/homingStatus";
import { isDuringMatch, isSetupPhase } from "@/lib/phase";
import { MALFORMED } from "@/lib/protocol";
import type { ManualState, OperationMode } from "@/lib/protocol";
import { isRestartFromTop, sequenceKind } from "@/lib/sequenceStatus";

interface RobotControlProps {
  robotKey: string;
  label: string;
}

export function RobotControl({ robotKey, label }: RobotControlProps) {
  const states = useRobotStates();
  const { matchState, connected, eStopActive, serverInfo, homing } = useRobotStatus();
  const { sendOrReport } = useRobotCommands();
  const state = states[robotKey];
  const [restartConfirmOpen, setRestartConfirmOpen] = useState(false);

  const handleTrigger = () => sendOrReport({ type: "trigger", robot: robotKey }, "トリガー");
  const handleJump = (stepIndex: number) =>
    sendOrReport(
      { type: "sequence_jump", robot: robotKey, step_index: stepIndex },
      "ステップジャンプ",
    );
  const handleStop = () => sendOrReport({ type: "sequence_stop", robot: robotKey }, "通常停止");
  const handleStart = () =>
    sendOrReport({ type: "sequence_start", robot: robotKey }, "シーケンス開始");
  const handleMode = (mode: OperationMode) =>
    sendOrReport({ type: "set_operation_mode", robot: robotKey, mode }, "操作モードの切り替え");
  const handleReenergize = () =>
    sendOrReport({ type: "reenergize_motors", robot: robotKey }, "再励磁");

  const inMatch = isDuringMatch(matchState.phase);
  const setupPhase = isSetupPhase(matchState.phase);
  const blockedLabel =
    matchState.phase === "finished"
      ? "試合終了"
      : matchState.phase === MALFORMED
        ? "フェーズ不明"
        : "準備中";
  const sequenceBlockedReason = connected ? null : "切断中のため送信できません";

  const kind = state ? sequenceKind(state) : null;

  const stepJumpBlockedReason =
    sequenceBlockedReason ??
    (!inMatch ? "試合中のみ操作可" : kind === "running" ? "停止してから選択" : null);

  const manual: ManualState = state?.manual ?? { mode: "sequence", axes: [] };
  const inManual = manual.mode === "manual";

  const modeBlockedReason = connected ? null : "切断中のため切り替えできません";
  const manualBlockedReason = !connected
    ? "切断中のため操作できません"
    : eStopActive
      ? "緊急停止中は手動操縦できません"
      : null;

  const needsRestartConfirm = state ? isRestartFromTop(state) : false;
  const requestStart = () => {
    if (needsRestartConfirm) setRestartConfirmOpen(true);
    else handleStart();
  };
  const confirmRestart = () => {
    setRestartConfirmOpen(false);
    handleStart();
  };

  useHotkeys(
    {
      " ": () => {
        if (!inMatch || !state || inManual) return;
        if (kind === "waiting_trigger") handleTrigger();
        else if (kind === "idle") requestStart();
      },
    },
    inMatch && !inManual,
  );

  if (!state) {
    return (
      <Page className="flex flex-col items-center justify-center">
        <Panel legend={label} className="flex-none">
          <p className="text-base-content/70">データ未受信 — 接続待機中...</p>
        </Panel>
      </Page>
    );
  }

  const modeSwitch = (
    <ModeSwitch
      mode={manual.mode}
      onChange={handleMode}
      blockedReason={modeBlockedReason}
      sequenceName={state.sequence}
      totalSteps={setupPhase && inManual ? state.total_steps : null}
    />
  );

  const manualPanel = (
    <ManualPanel
      robotKey={robotKey}
      manual={manual}
      blockedReason={manualBlockedReason}
      sendOrReport={sendOrReport}
    />
  );

  // どちらのモードでも出す。選択は機体を動かさず、次の吸着ステップから効くだけなので
  // 塞ぐ理由は切断だけ
  const suctionPanel =
    state.suction === undefined || state.suction === null ? null : (
      <SuctionPadPanel
        robotKey={robotKey}
        suction={state.suction}
        blockedReason={connected ? null : "切断中のため変更できません"}
        sendOrReport={sendOrReport}
      />
    );

  // 手動中はサーバーが必ず拒むので、無効ボタンではなく配られた拒否理由だけを出す
  const homingReason = homingStatus(homing, connected).reasonLabel;
  const homingPanel =
    inManual && homingReason === null ? null : (
      <Panel legend="零点合わせ" className="shrink-0" bodyClassName="gap-1.5">
        {inManual ? (
          <p className="text-base-content/70">{homingReason}</p>
        ) : (
          <div className="flex flex-wrap items-center gap-2">
            <HomingButtons robot={robotKey} />
          </div>
        )}
        <HomingPanel robot={robotKey} />
      </Panel>
    );

  const subsystemPanel = (open: boolean, className?: string) => (
    <Panel legend="機体状態" className={className}>
      <SubsystemStatus
        health={state.health}
        motors={state.motors}
        safety={state.safety}
        sensors={state.sensors}
        connected={connected}
        tempThresholds={tempThresholdsOf(serverInfo)}
        defaultOpen={open}
        onReenergize={handleReenergize}
      />
    </Panel>
  );

  const stepPanel = (
    <Panel
      legend="ステップ"
      className="min-h-0 flex-1"
      bodyClassName="p-0"
      actions={
        stepJumpBlockedReason ? (
          <span className="text-[0.85em] text-base-content/60">{stepJumpBlockedReason}</span>
        ) : null
      }
    >
      <SequenceStepList
        steps={state.steps ?? []}
        stepIndex={state.step_index}
        waitingTrigger={state.waiting_trigger}
        onJump={handleJump}
        disabled={stepJumpBlockedReason !== null}
      />
    </Panel>
  );

  if (setupPhase) {
    const openSubsystemPanel = subsystemPanel(true, "min-h-0 flex-1");
    return (
      <Page className="flex flex-col">
        {modeSwitch}
        <div className="grid min-h-0 flex-1 grid-cols-[minmax(0,1fr)_minmax(19rem,26rem)] gap-2">
          <div className="flex min-h-0 flex-col gap-2">
            {suctionPanel}
            {homingPanel}
            {inManual ? manualPanel : openSubsystemPanel}
          </div>
          {inManual ? openSubsystemPanel : stepPanel}
        </div>
      </Page>
    );
  }

  return (
    <Page className="flex flex-col">
      {modeSwitch}
      <div className="grid min-h-0 flex-1 grid-cols-[minmax(0,1fr)_minmax(17rem,21rem)] gap-2">
        {inManual ? (
          <div className="flex min-h-0 flex-col gap-2">
            {suctionPanel}
            {manualPanel}
          </div>
        ) : (
          <div className="flex min-h-0 flex-col gap-2">
            <ActionPanel
              state={state}
              inMatch={inMatch}
              blockedLabel={blockedLabel}
              blockedReason={sequenceBlockedReason}
              onStart={requestStart}
              onStop={handleStop}
              onTrigger={handleTrigger}
            />

            <AlwaysManualPanel
              robotKey={robotKey}
              manual={manual}
              blockedReason={manualBlockedReason}
              sendOrReport={sendOrReport}
            />

            {suctionPanel}

            {stepPanel}
          </div>
        )}

        <div className="flex min-h-0 flex-col gap-2">
          <MatchTimer timer={matchState.timer} />

          {subsystemPanel(inManual, inManual ? "min-h-0 flex-1" : undefined)}
        </div>
      </div>

      <Modal
        open={restartConfirmOpen}
        onClose={() => setRestartConfirmOpen(false)}
        tone="danger"
        title="先頭から再開"
        footer={
          <>
            <Button onClick={() => setRestartConfirmOpen(false)}>キャンセル</Button>
            <Button tone="warn" onClick={confirmRestart}>
              先頭から実行
            </Button>
          </>
        }
      >
        <p>
          ステップ {state.step_index + 1} で停止しています。
          <span className="font-medium">ステップ 1 へ戻って全工程を走り直します。</span>
        </p>
        <p className="mt-2 text-base-content/70">
          中断した位置から続けるときは、ステップ一覧から再開するステップを選んでください。
        </p>
        <p className="mt-2 flex items-center gap-1.5 text-warning">
          <Icon as={TriangleAlert} />
          現在の姿勢のまま先頭の動作が走ります。物理状態が安全であることを必ず確認してください。
        </p>
      </Modal>
    </Page>
  );
}
