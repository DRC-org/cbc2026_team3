import { ListChecks } from "lucide-react";
import { useState } from "react";

import { SubsystemStatus } from "@/components/diagnostics/SubsystemStatus";
import { MotorCheckButton } from "@/components/motorcheck/MotorCheckButton";
import { MotorCheckPanel } from "@/components/motorcheck/MotorCheckPanel";
import { MotorCheckSummary } from "@/components/motorcheck/MotorCheckSummary";
import { ActionPanel } from "@/components/operator/ActionPanel";
import { Checklist } from "@/components/operator/Checklist";
import { MatchTimer } from "@/components/operator/MatchTimer";
import { SequenceStepList } from "@/components/operator/SequenceStepList";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { Page } from "@/components/ui/Page";
import { Panel } from "@/components/ui/Panel";
import { useRobotCommands, useRobotStates, useRobotStatus } from "@/context/RobotContext";
import { useHotkeys } from "@/hooks/useHotkeys";
import { isDuringMatch, isSetupPhase } from "@/lib/phase";
import type { ChecklistRole } from "@/lib/protocol";
import { sequenceKind } from "@/lib/sequenceStatus";

interface RobotControlProps {
  robotKey: string;
  label: string;
}

export function RobotControl({ robotKey, label }: RobotControlProps) {
  const states = useRobotStates();
  const { matchState } = useRobotStatus();
  const { send } = useRobotCommands();
  const state = states[robotKey];
  const [healthCheckOpen, setHealthCheckOpen] = useState(false);

  const handleTrigger = () => send({ type: "trigger", robot: robotKey });
  const handleJump = (stepIndex: number) =>
    send({ type: "sequence_jump", robot: robotKey, step_index: stepIndex });
  const handleStop = () => send({ type: "sequence_stop", robot: robotKey });
  const handleStart = () => send({ type: "sequence_start", robot: robotKey });

  // シーケンス操作が許されるのは試合中のみ (サーバー側のフェーズゲートと対応)
  const inMatch = isDuringMatch(matchState.phase);
  const setupPhase = isSetupPhase(matchState.phase);
  const blockedLabel = matchState.phase === "finished" ? "試合終了" : "準備中";

  // 実行状態はサーバー配信の running が唯一の根拠。step_index からの推測をしない
  const kind = state ? sequenceKind(state) : null;

  // Space に主操作を集約する。ルーターは表示中のタブしか描画しないので、
  // 表示中のロボットにだけ届く。トリガー待ちなら NEXT、待機中なら START に解決する
  useHotkeys(
    {
      " ": () => {
        if (!inMatch || !state) return;
        if (kind === "waiting_trigger") handleTrigger();
        else if (kind === "idle") handleStart();
      },
    },
    inMatch,
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

  // --- セッティングタイム -------------------------------------------------
  // 操縦者の仕事は 2 つだけ: 指差喚呼を終えることと、アクチュエータを動かして確かめること。
  // 以前はここに操作不能な SEQUENCE PREVIEW が画面の半分を占めていた。試合前に
  // 一覧を見たくなることはあるが、それは「今やること」ではないのでモーダルへ退避した。
  if (setupPhase) {
    return (
      <>
        <Page className="grid grid-cols-[minmax(0,1fr)_minmax(19rem,26rem)]">
          <Checklist
            checklistRole={robotKey as ChecklistRole}
            title={`${label} セッティング指差喚呼`}
          />

          <div className="flex min-h-0 flex-col gap-2">
            {/* 動作確認は準備フェーズの主要アクション。以前は診断カラムの最下段に
                埋もれていたため、独立した枠で上に出す */}
            <Panel
              legend="アクチュエータ動作確認"
              className="shrink-0"
              actions={<MotorCheckSummary robotName={robotKey} />}
            >
              <div className="flex flex-wrap items-center gap-2">
                <MotorCheckButton
                  robotName={robotKey}
                  onPanelOpen={() => setHealthCheckOpen(true)}
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
      <Page className="grid grid-cols-[minmax(0,1fr)_minmax(17rem,21rem)]">
        {/* 左は操作面。主役を内容ぶんの高さに留め、余った縦はステップ一覧へ渡す */}
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

        {/* 右は参照面。試合時間は操作面へ置かない — 主操作 (ActionPanel) の位置は
            状態によって動かさない約束なので、上に何かを積むと押す前に探し直しになる。
            診断は平常時 1 行に畳み、異常が出たときだけ自分から開く */}
        <div className="flex min-h-0 flex-col gap-2">
          <MatchTimer timer={matchState.timer} />

          <Panel legend="機体状態" className="self-start">
            <SubsystemStatus health={state.health} motors={state.motors} safety={state.safety} />
          </Panel>
        </div>
      </Page>
      {motorCheckPanel}
    </>
  );
}
