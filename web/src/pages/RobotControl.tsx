import { TriangleAlert } from "lucide-react";
import { useState } from "react";

import { SubsystemStatus } from "@/components/diagnostics/SubsystemStatus";
import { HomingButtons } from "@/components/homing/HomingButtons";
import { HomingPanel } from "@/components/homing/HomingPanel";
import { ReturnHomeButton } from "@/components/homing/ReturnHomeButton";
import { SwitchDistanceButton } from "@/components/homing/SwitchDistanceButton";
import { CourtSettings } from "@/components/monitor/CourtSettings";
import { StartGate } from "@/components/monitor/StartGate";
import { ActionPanel } from "@/components/operator/ActionPanel";
import { AlwaysManualPanel } from "@/components/operator/AlwaysManualPanel";
import { ManualPanel } from "@/components/operator/ManualPanel";
import { MatchTimer } from "@/components/operator/MatchTimer";
import { ModeSwitch } from "@/components/operator/ModeSwitch";
import { NudgePanel } from "@/components/operator/NudgePanel";
import { PositionsReloadButton } from "@/components/operator/PositionsReloadButton";
import { SequenceStepList } from "@/components/operator/SequenceStepList";
import { SuctionPadPanel } from "@/components/operator/SuctionPadPanel";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { Modal } from "@/components/ui/Modal";
import { Page } from "@/components/ui/Page";
import { Panel } from "@/components/ui/Panel";
import { useRobotCommands, useRobotStates, useRobotStatus } from "@/context/RobotContext";
import { useHotkeys } from "@/hooks/useHotkeys";
import { cx } from "@/lib/cx";
import { tempThresholdsOf } from "@/lib/healthVerdict";
import { isDuringMatch, isSetupPhase } from "@/lib/phase";
import { MALFORMED } from "@/lib/protocol";
import type { ManualState, OperationMode } from "@/lib/protocol";
import { hasSwitchMeasure } from "@/lib/robots";
import { isRestartFromTop, sequenceKind } from "@/lib/sequenceStatus";

interface RobotControlProps {
  robotKey: string;
  label: string;
}

export function RobotControl({ robotKey, label }: RobotControlProps) {
  const states = useRobotStates();
  const { matchState, connected, eStopActive, serverInfo } = useRobotStatus();
  const { sendOrReport, matchStart } = useRobotCommands();
  const state = states[robotKey];
  const [restartConfirmOpen, setRestartConfirmOpen] = useState(false);
  const [forceConfirmOpen, setForceConfirmOpen] = useState(false);

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
  const handleForce = () => {
    setForceConfirmOpen(false);
    sendOrReport({ type: "sequence_force_step", robot: robotKey }, "歯止めを外して続行");
  };

  const inMatch = isDuringMatch(matchState.phase);
  const setupPhase = isSetupPhase(matchState.phase);
  const blockedLabel =
    matchState.phase === "finished"
      ? "試合終了"
      : matchState.phase === MALFORMED
        ? "フェーズ不明"
        : "準備中";
  // コート確定が要るかはサーバーが決める (state.court_required)。UI が軸名から導き直さない
  const courtUnset = state?.court_required === true && matchState.court === null;

  // 切断中・緊急停止中・コート未設定は ConnectionBanner / EStopBanner / ヘッダーのコートバッジが
  // 言うので、パネルには理由を配らず無効化だけ伝える
  const sequenceBlocked = !connected || courtUnset;
  const modeBlocked = !connected;

  const kind = state ? sequenceKind(state) : null;

  // パネル固有の理由は他が言っていないのでそのパネルにだけ出す
  const stepJumpReason = !inMatch
    ? "試合中のみ操作可"
    : kind === "running"
      ? "停止してから選択"
      : null;
  const stepJumpBlocked = sequenceBlocked || stepJumpReason !== null;

  const manual: ManualState = state?.manual ?? { mode: "sequence", axes: [] };
  const inManual = manual.mode === "manual";

  const manualBlocked = !connected || eStopActive || courtUnset;

  // 半自動の手動操作は動いている最中だけ塞ぐ (サーバーの _allow_manual_in_sequence と対)。
  // トリガー待ちは ±2mm の微調整だけ、止まっているあいだは全部通る
  const sequenceMoving = kind === "running";
  const nudgeReason = sequenceMoving ? "移動中は不可" : null;
  const nudgeBlocked = manualBlocked || nudgeReason !== null;

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
          <p className="text-base-content/70">データ未受信</p>
        </Panel>
      </Page>
    );
  }

  const modeSwitch = (
    <ModeSwitch
      mode={manual.mode}
      onChange={handleMode}
      blocked={modeBlocked}
      sequenceName={state.sequence}
    />
  );

  // パッドの弁は SuctionPadPanel が持つので、同じ 6 個が手動操縦にも並ばないよう外す。
  // パネルが出ない配信（MALFORMED / null）では外さない —— 操作口ごと消えてはならない
  const suctionPadAxes =
    state.suction === undefined || state.suction === null || state.suction === MALFORMED
      ? undefined
      : state.suction.pads.map((pad) => pad.axis);

  const manualPanel = (
    <ManualPanel
      robotKey={robotKey}
      manual={manual}
      blocked={manualBlocked}
      sendOrReport={sendOrReport}
      excludeAxes={suctionPadAxes}
    />
  );

  // どちらのモードでも出す。半自動では次の吸着で使う弁の宣言（機体を動かさない）、
  // 手動ではその場の開閉。置くのは機体状態と同じ列 —— 左は機体を動かす面だけに寄せる
  const suctionPanel =
    state.suction === undefined || state.suction === null ? null : (
      <SuctionPadPanel
        robotKey={robotKey}
        suction={state.suction}
        manual={manual}
        blocked={inManual ? manualBlocked : !connected}
        directBlocked={manualBlocked}
        sendOrReport={sendOrReport}
      />
    );

  // コートが要る台 (サーバーが court_required で言う) は、この画面からも選べるようにする。
  // 試合のリセットは Monitor だけが持つので RESET は出さない
  const courtPanel = state.court_required === true ? <CourtSettings /> : null;

  // 位置を控える面は消した (2026-09-12)。yaml を読み直す口だけ手動の列に残す
  const capturePanel = !inManual ? null : (
    <PositionsReloadButton
      robotKey={robotKey}
      reload={state.positions_reload ?? null}
      sendOrReport={sendOrReport}
    />
  );

  // 測定系はボタン 2 つだけ。箱で囲まず吸着パッドの上に小さく並べる。
  // 試合中も零点合わせの在り処は画面から読めるようにする (押せないときは理由が出る)
  const measurePanel = (withSwitchDistance: boolean) => (
    <div className="flex shrink-0 flex-col gap-1.5">
      <div className="flex flex-wrap items-center gap-2">
        <HomingButtons robot={robotKey} />
        <ReturnHomeButton robot={robotKey} />
        {withSwitchDistance && hasSwitchMeasure(robotKey) ? (
          <SwitchDistanceButton robot={robotKey} />
        ) : null}
      </div>
      <HomingPanel robot={robotKey} />
    </div>
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
        concise
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
        stepJumpReason ? (
          <span className="text-[0.85em] text-base-content/60">{stepJumpReason}</span>
        ) : null
      }
    >
      <SequenceStepList
        steps={state.steps ?? []}
        stepIndex={state.step_index}
        waitingTrigger={state.waiting_trigger}
        onJump={handleJump}
        disabled={stepJumpBlocked}
      />
    </Panel>
  );

  if (setupPhase) {
    const openSubsystemPanel = subsystemPanel(true, "min-h-0 flex-1");
    const statusColumn = (
      <div className="flex min-h-0 flex-col gap-2">
        {courtPanel}
        {suctionPanel}
        {openSubsystemPanel}
      </div>
    );
    return (
      <Page className="flex flex-col">
        <StartGate onStart={matchStart} />
        {modeSwitch}
        <div className="grid min-h-0 flex-1 grid-cols-[minmax(0,1fr)_minmax(19rem,26rem)] gap-2">
          {inManual ? (
            <div className="flex min-h-0 flex-col gap-2">
              {manualPanel}
              {capturePanel}
            </div>
          ) : (
            <div className="flex min-h-0 flex-col gap-2">
              {courtPanel}
              {measurePanel(true)}
              {suctionPanel}
              {openSubsystemPanel}
            </div>
          )}
          {inManual ? statusColumn : stepPanel}
        </div>
      </Page>
    );
  }

  return (
    <Page className="flex flex-col">
      {modeSwitch}
      {/* 半自動はステップに専用の列を割く —— 縦積みだと数行しか見えない */}
      <div
        className={cx(
          "grid min-h-0 flex-1 gap-2",
          inManual
            ? "grid-cols-[minmax(0,1fr)_minmax(17rem,21rem)]"
            : "grid-cols-[minmax(0,1fr)_minmax(15rem,20rem)_minmax(15rem,19rem)]",
        )}
      >
        {inManual ? (
          <div className="flex min-h-0 flex-col gap-2">
            {manualPanel}
            {capturePanel}
          </div>
        ) : (
          <>
            <div className="flex min-h-0 flex-col gap-2">
              <ActionPanel
                state={state}
                inMatch={inMatch}
                blockedLabel={blockedLabel}
                blocked={sequenceBlocked}
                onStart={requestStart}
                onStop={handleStop}
                onTrigger={handleTrigger}
                onForce={() => setForceConfirmOpen(true)}
              />

              <AlwaysManualPanel
                robotKey={robotKey}
                manual={manual}
                blocked={manualBlocked}
                sendOrReport={sendOrReport}
                excludeAxes={suctionPadAxes}
              />

              {/* 常に出す。押せないときは理由を出す —— 隠すと在り処が画面から読めない */}
              <NudgePanel
                robotKey={robotKey}
                manual={manual}
                blocked={nudgeBlocked}
                blockedReason={nudgeReason}
                centerBlocked={manualBlocked}
                sendOrReport={sendOrReport}
              />

              {measurePanel(false)}
            </div>

            <div className="flex min-h-0 flex-col">{stepPanel}</div>
          </>
        )}

        <div className="flex min-h-0 flex-col gap-2">
          {courtPanel}
          <MatchTimer timer={matchState.timer} />

          {suctionPanel}

          {subsystemPanel(inManual, inManual ? "min-h-0 flex-1" : undefined)}
        </div>
      </div>

      <Modal
        open={forceConfirmOpen}
        onClose={() => setForceConfirmOpen(false)}
        tone="danger"
        title="歯止めを外して続行"
        footer={
          <>
            <Button onClick={() => setForceConfirmOpen(false)}>キャンセル</Button>
            <Button tone="warn" onClick={handleForce}>
              歯止めを外して走らせる
            </Button>
          </>
        }
      >
        <p>
          ステップ {state.step_index + 1}「{state.last_error?.step ?? "—"}」を、
          <span className="font-medium">可動端の歯止めを外して走らせ直します。</span>
        </p>
        <p className="mt-2 text-base-content/70">
          外れるのはこの 1 ステップのあいだだけで、次のステップからは元に戻ります。
        </p>
        <p className="mt-2 flex items-center gap-1.5 text-warning">
          <Icon as={TriangleAlert} />
          リミットスイッチを踏んでも止まりません。機構が端に当たらないことを目で確かめてから押してください。
        </p>
      </Modal>

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
