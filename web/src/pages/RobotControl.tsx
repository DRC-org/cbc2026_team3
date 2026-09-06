import { TriangleAlert } from "lucide-react";
import { useState } from "react";

import { SubsystemStatus } from "@/components/diagnostics/SubsystemStatus";
import { ActionPanel } from "@/components/operator/ActionPanel";
import { ManualPanel } from "@/components/operator/ManualPanel";
import { MatchTimer } from "@/components/operator/MatchTimer";
import { ModeSwitch } from "@/components/operator/ModeSwitch";
import { SequenceStepList } from "@/components/operator/SequenceStepList";
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
import { isRestartFromTop, sequenceKind } from "@/lib/sequenceStatus";

interface RobotControlProps {
  robotKey: string;
  label: string;
}

export function RobotControl({ robotKey, label }: RobotControlProps) {
  const states = useRobotStates();
  const { matchState, connected, eStopActive, serverInfo } = useRobotStatus();
  const { send, sendOrReport } = useRobotCommands();
  const state = states[robotKey];
  const [restartConfirmOpen, setRestartConfirmOpen] = useState(false);

  // **主操作は戻り値を捨てない。** 切断中の `send` は false を返して黙るので、
  // 捨てると「押したのにボタンは有効なまま・機体は動かない・トーストも出ない」になる。
  // 試合中に最も多く押す NEXT を含む全操作がその形だった
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
  // 可否の判定は持たない。押せば送るだけで、拒否はサーバーが理由付きで返す
  const handleReenergize = () =>
    sendOrReport({ type: "reenergize_motors", robot: robotKey }, "再励磁");

  // シーケンス操作が許されるのは試合中のみ (サーバー側のフェーズゲートと対応)
  const inMatch = isDuringMatch(matchState.phase);
  const setupPhase = isSetupPhase(matchState.phase);
  const blockedLabel =
    matchState.phase === "finished"
      ? "試合終了"
      : matchState.phase === MALFORMED
        ? "フェーズ不明"
        : "準備中";
  // 可否の正はサーバーだが、切断中は届かないので画面側でしか分からない。
  // 塞がずに押させると「押したのに何も起きない」だけが操縦者に残る
  const sequenceBlockedReason = connected ? null : "切断中のため送信できません";

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

  /**
   * START が「先頭へ戻して全工程を走り直す」意味になっているか。
   *
   * `sequence_stop` は `step_index` を保持したまま降りるので、画面は中断位置を
   * 出したまま START を差し出す。そこで押すと中断姿勢のまま先頭の動作が走る ——
   * `sequence_jump` (同じ「任意ステップから再開」) が確認を挟むのに、より危険な
   * こちらだけが素通しだった。**Space も同じ経路を通す** (キー 1 打で全工程が
   * 走り出す方が、ボタンより危ない)。
   */
  const needsRestartConfirm = state ? isRestartFromTop(state) : false;
  const requestStart = () => {
    if (needsRestartConfirm) setRestartConfirmOpen(true);
    else handleStart();
  };
  const confirmRestart = () => {
    setRestartConfirmOpen(false);
    handleStart();
  };

  // Space に主操作を集約する。ルーターは表示中のタブしか描画しないので、
  // 表示中のロボットにだけ届く。トリガー待ちなら NEXT、待機中なら START に解決する
  // 手動モード中は無効化する。誤爆した Space が sequence_start になると、
  // 手動で機構を動かしている最中にシーケンスが走り出す
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

  /**
   * 機体状態のパネル。**既定の開閉だけが役割で違う。**
   *
   * 準備中は配線確認が目的のフェーズなので開いた状態から始め、試合中は
   * 平常時 1 行へ畳む (操縦者は機体を見ており、画面へ視線を戻すのは一瞬しかない)。
   * ただし手動中は畳まない —— 機体を直接動かしている最中は、その前提が成り立たない。
   */
  const subsystemPanel = (open: boolean, className: string) => (
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

  // --- セッティングタイム -------------------------------------------------
  // 指差喚呼と動作確認は Monitor の設定面へ移した。指差喚呼は操縦者 2 名が
  // 同じ場所に立つので二度読み上げになっていたため、動作確認は両ハンドを 1 本の
  // シーケンスで駆動するので機体ごとの入口が意味を失ったため。
  // ここに残るのは手動操縦と、その手元で見る機体状態。
  if (setupPhase) {
    return (
      <Page className="flex flex-col">
        {modeSwitch}
        <div
          className={cx(
            "grid min-h-0 flex-1 gap-2",
            // 手動中だけ操作面のために左列を開ける。半自動の準備中はこの画面に
            // 操作が無いので、参照面を 1 列に広げる
            inManual ? "grid-cols-[minmax(0,1fr)_minmax(19rem,26rem)]" : "grid-cols-1",
          )}
        >
          {inManual ? manualPanel : null}

          <div className="flex min-h-0 flex-col gap-2">
            {subsystemPanel(true, "min-h-0 flex-1")}

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
    );
  }

  // --- 試合中 / 試合終了 --------------------------------------------------
  // 操縦者は機体を見ている。画面へ視線を戻すのは一瞬で、答えるべき問いは
  // 「今 NEXT を押すのか」「押すと何が起きるか」の 2 つだけ。
  // 左を操作面、右を参照面に割り切り、参照面の診断は平常時 1 行へ畳む。
  return (
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
              blockedReason={sequenceBlockedReason}
              onStart={requestStart}
              onStop={handleStop}
              onTrigger={handleTrigger}
            />

            <Panel
              legend="ステップ"
              className="min-h-0 flex-1"
              bodyClassName="p-0"
              actions={
                <span className="text-[0.85em] text-base-content/60">
                  {sequenceBlockedReason ?? (inMatch ? "クリックで再開" : "試合中のみ操作可")}
                </span>
              }
            >
              <SequenceStepList
                steps={state.steps ?? []}
                stepIndex={state.step_index}
                waitingTrigger={state.waiting_trigger}
                onJump={handleJump}
                disabled={!inMatch || sequenceBlockedReason !== null}
              />
            </Panel>
          </div>
        )}

        {/* 右は参照面。試合時間は操作面へ置かない — 主操作 (ActionPanel) の位置は
            状態によって動かさない約束なので、上に何かを積むと押す前に探し直しになる。
            診断は平常時 1 行に畳み、異常が出たときだけ自分から開く */}
        <div className="flex min-h-0 flex-col gap-2">
          <MatchTimer timer={matchState.timer} />

          {subsystemPanel(inManual, inManual ? "min-h-0 flex-1" : "self-start")}
        </div>
      </div>

      {/* 中断位置から押した START の確認。**モーダルの中身は「押すと何が起きるか」**
          を書く場所で、ここでは「先頭へ戻る」ことと「中断姿勢のまま先頭の動作が走る」
          ことがそれに当たる。**途中から再開したいときの導線もここで示す** —
          示さないと、操縦者は他に手が無いと思って全工程のやり直しを選ぶ */}
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
