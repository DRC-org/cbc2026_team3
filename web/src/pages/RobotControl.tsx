import { ListChecks } from "lucide-react";
import { useState } from "react";

import { SubsystemStatus } from "@/components/diagnostics/SubsystemStatus";
import { MotorCheckButton } from "@/components/motorcheck/MotorCheckButton";
import { MotorCheckPanel } from "@/components/motorcheck/MotorCheckPanel";
import { MotorCheckSummary } from "@/components/motorcheck/MotorCheckSummary";
import { ActionPanel } from "@/components/operator/ActionPanel";
import { ManualPanel } from "@/components/operator/ManualPanel";
import { MatchTimer } from "@/components/operator/MatchTimer";
import { ModeSwitch } from "@/components/operator/ModeSwitch";
import { SequenceStepList } from "@/components/operator/SequenceStepList";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { Page } from "@/components/ui/Page";
import { Panel } from "@/components/ui/Panel";
import { useRobotCommands, useRobotStates, useRobotStatus } from "@/context/RobotContext";
import { useHotkeys } from "@/hooks/useHotkeys";
import { cx } from "@/lib/cx";
import { isDuringMatch, isSetupPhase } from "@/lib/phase";
import type { ManualState, OperationMode } from "@/lib/protocol";
import { sequenceKind } from "@/lib/sequenceStatus";

interface RobotControlProps {
  robotKey: string;
  label: string;
}

export function RobotControl({ robotKey, label }: RobotControlProps) {
  const states = useRobotStates();
  const { matchState, connected, eStopActive } = useRobotStatus();
  const { send } = useRobotCommands();
  const state = states[robotKey];
  const [healthCheckOpen, setHealthCheckOpen] = useState(false);

  const handleTrigger = () => send({ type: "trigger", robot: robotKey });
  const handleJump = (stepIndex: number) =>
    send({ type: "sequence_jump", robot: robotKey, step_index: stepIndex });
  const handleStop = () => send({ type: "sequence_stop", robot: robotKey });
  const handleStart = () => send({ type: "sequence_start", robot: robotKey });
  const handleMode = (mode: OperationMode) =>
    send({ type: "set_operation_mode", robot: robotKey, mode });

  // シーケンス操作が許されるのは試合中のみ (サーバー側のフェーズゲートと対応)
  const inMatch = isDuringMatch(matchState.phase);
  const setupPhase = isSetupPhase(matchState.phase);
  const blockedLabel = matchState.phase === "finished" ? "試合終了" : "準備中";

  // 実行状態はサーバー配信の running が唯一の根拠。step_index からの推測をしない
  const kind = state ? sequenceKind(state) : null;

  // 操作モードもサーバーが正。配信を受け取るまでは半自動として描く
  // (機体を直接動かせる状態を、確証のないまま画面へ出さない)
  const manual: ManualState = state?.manual ?? { mode: "sequence", axes: [] };
  const inManual = manual.mode === "manual";

  // 可否の正はサーバー (lib/commands.py) で、ここは押す前に理由を出すだけ。
  // **フェーズでは塞がない** — 調整は準備中に、シーケンスからの退避は試合中に要る。
  //
  // **モード切替と手動指令は別の理由で塞がる。** サーバーは切替 (機体を動かさない) を
  // 緊急停止中も通し、指令 (目標値を送る) だけを塞ぐ。ここを 1 つにまとめると、
  // サーバーが受け付ける操作を画面が殺す — 停止中に手動へ寄せて解除と同時に
  // 動かす、という手順が取れなくなる
  const modeBlockedReason = connected ? null : "切断中のため切り替えできません";
  const manualBlockedReason = !connected
    ? "切断中のため操作できません"
    : eStopActive
      ? "緊急停止中は手動操縦できません"
      : null;

  // Space に主操作を集約する。ルーターは表示中のタブしか描画しないので、
  // 表示中のロボットにだけ届く。トリガー待ちなら NEXT、待機中なら START に解決する
  // 手動モード中は無効化する。誤爆した Space が sequence_start になると、
  // 手動で機構を動かしている最中にシーケンスが走り出す
  useHotkeys(
    {
      " ": () => {
        if (!inMatch || !state || inManual) return;
        if (kind === "waiting_trigger") handleTrigger();
        else if (kind === "idle") handleStart();
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

  const motorCheckPanel = (
    <MotorCheckPanel
      robotName={robotKey}
      isOpen={healthCheckOpen}
      onOpenChange={setHealthCheckOpen}
    />
  );

  // モード帯はどのフェーズでも同じ位置に出す。「今この画面から機体を直接
  // 動かせるか」は、準備中も試合中も同じ場所で読めなければならない
  const modeSwitch = (
    <ModeSwitch mode={manual.mode} onChange={handleMode} blockedReason={modeBlockedReason} />
  );

  // 手動の操作面。半自動側の主役 (動作確認 / ActionPanel) と同じ列を占める
  const manualPanel = (
    <ManualPanel
      robotKey={robotKey}
      manual={manual}
      blockedReason={manualBlockedReason}
      send={send}
    />
  );

  // --- セッティングタイム -------------------------------------------------
  // 指差喚呼は Monitor の設定面へ移した (操縦者 2 名が同じ場所に立つので、
  // 同じ機体を 2 つの画面で二度読み上げるだけになっていた)。ここに残るのは
  // 「アクチュエータを動かして確かめること」と、そのための手動操縦。
  if (setupPhase) {
    return (
      <>
        <Page className="flex flex-col">
          {modeSwitch}
          <div
            className={cx(
              "grid min-h-0 flex-1 gap-2",
              // 手動中だけ操作面のために左列を開ける。半自動の準備中はこの画面に
              // 操作が無いので (指差喚呼は Monitor)、参照面を 1 列に広げる
              inManual ? "grid-cols-[minmax(0,1fr)_minmax(19rem,26rem)]" : "grid-cols-1",
            )}
          >
            {inManual ? manualPanel : null}

            <div className="flex min-h-0 flex-col gap-2">
              {/* 動作確認は準備フェーズの主要アクション。手動モード中はサーバーが
                  拒否するので、押す前に理由を出せるようボタン側で塞ぐ */}
              <Panel
                legend="アクチュエータ動作確認"
                className="shrink-0"
                actions={<MotorCheckSummary robotName={robotKey} />}
              >
                <div className="flex flex-wrap items-center gap-2">
                  <MotorCheckButton
                    robotName={robotKey}
                    onPanelOpen={() => setHealthCheckOpen(true)}
                    blockedReason={inManual ? "手動操縦モードのため実行できません" : null}
                  />
                  <Button onClick={() => setHealthCheckOpen(true)}>
                    <Icon as={ListChecks} />
                    結果を表示
                  </Button>
                </div>
              </Panel>

              <Panel legend="機体状態" className="min-h-0 flex-1">
                <SubsystemStatus
                  health={state.health}
                  motors={state.motors}
                  safety={state.safety}
                  defaultOpen
                />
              </Panel>

              <Panel legend="シーケンス" className="shrink-0">
                <div className="flex items-center gap-2">
                  <span className="min-w-0 flex-1 truncate font-mono">{state.sequence}</span>
                  <span className="shrink-0 text-base-content/70">
                    全 {state.total_steps} ステップ
                  </span>
                </div>
              </Panel>
            </div>
          </div>
        </Page>
        {motorCheckPanel}
      </>
    );
  }

  // --- 試合中 / 試合終了 --------------------------------------------------
  // 操縦者は機体を見ている。画面へ視線を戻すのは一瞬で、答えるべき問いは
  // 「今 NEXT を押すのか」「押すと何が起きるか」の 2 つだけ。
  // 左を操作面、右を参照面に割り切り、参照面の診断は平常時 1 行へ畳む。
  return (
    <>
      <Page className="flex flex-col">
        {modeSwitch}
        <div className="grid min-h-0 flex-1 grid-cols-[minmax(0,1fr)_minmax(17rem,21rem)] gap-2">
          {/* 左は操作面。主役を内容ぶんの高さに留め、余った縦はステップ一覧へ渡す。
            手動中はここを手動パネルへ明け渡す — 同じ列に 2 つの操作面が並ぶと、
            どちらの指令が機体へ届くのかが画面から読めなくなる */}
          {inManual ? (
            manualPanel
          ) : (
            <div className="flex min-h-0 flex-col gap-2">
              <ActionPanel
                state={state}
                inMatch={inMatch}
                blockedLabel={blockedLabel}
                onStart={handleStart}
                onStop={handleStop}
                onTrigger={handleTrigger}
              />

              <Panel
                legend="ステップ"
                className="min-h-0 flex-1"
                bodyClassName="p-0"
                actions={
                  <span className="text-[0.85em] text-base-content/60">
                    {inMatch ? "クリックで再開" : "試合中のみ操作可"}
                  </span>
                }
              >
                <SequenceStepList
                  steps={state.steps ?? []}
                  stepIndex={state.step_index}
                  waitingTrigger={state.waiting_trigger}
                  onJump={handleJump}
                  disabled={!inMatch}
                />
              </Panel>
            </div>
          )}

          {/* 右は参照面。試合時間は操作面へ置かない — 主操作 (ActionPanel) の位置は
            状態によって動かさない約束なので、上に何かを積むと押す前に探し直しになる。
            診断は平常時 1 行に畳み、異常が出たときだけ自分から開く */}
          <div className="flex min-h-0 flex-col gap-2">
            <MatchTimer timer={matchState.timer} />

            {/* 手動中は畳まない。機体を直接動かしている最中は、平常時 1 行へ
              畳む前提 (操縦者は機体を見ており画面は一瞬しか見ない) が成り立たない */}
            <Panel legend="機体状態" className={inManual ? "min-h-0 flex-1" : "self-start"}>
              <SubsystemStatus
                health={state.health}
                motors={state.motors}
                safety={state.safety}
                defaultOpen={inManual}
              />
            </Panel>
          </div>
        </div>
      </Page>
      {motorCheckPanel}
    </>
  );
}
