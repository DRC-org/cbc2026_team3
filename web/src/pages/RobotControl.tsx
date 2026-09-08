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
  // この画面が送るものは**すべて** `sendOrReport` を通る。素の `send` を持ち出すと、
  // 戻り値を捨てる書き方が 1 経路だけ混ざっても気付けない
  const { sendOrReport } = useRobotCommands();
  const state = states[robotKey];
  const [restartConfirmOpen, setRestartConfirmOpen] = useState(false);

  // **主操作は戻り値を捨てない。** 切断中の `send` は false を返して黙るので、捨てると
  // 「押したのにボタンは有効なまま・機体は動かない・トーストも出ない」になる
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

  /**
   * ステップ一覧を押せない理由。null なら押せる。
   *
   * **可否と案内文をここ 1 つで決める** —— 別々に書くと「押せないのに『クリックで再開』と
   * 案内し続ける」状態が作れ、一覧の行は見た目がほぼ変わらないので操縦者からは故障と
   * 区別が付かない。**null なら何も描かない。**
   *
   * **駆動中を塞ぐ理由**はジャンプの確認が全画面モーダルだから (開いているあいだ
   * ヘッダーの EMG STOP がクリックできない)。**トリガー待ちは塞がない** ——
   * `require_trigger` で止まっている間、機体は動いておらず、そこは再開ステップを選ぶ
   * 本来の場面である。
   */
  const stepJumpBlockedReason =
    sequenceBlockedReason ??
    (!inMatch ? "試合中のみ操作可" : kind === "running" ? "停止してから選択" : null);

  // 操作モードもサーバーが正。配信を受け取るまでは半自動として描く
  // (機体を直接動かせる状態を、確証のないまま画面へ出さない)
  const manual: ManualState = state?.manual ?? { mode: "sequence", axes: [] };
  const inManual = manual.mode === "manual";

  // 可否の正はサーバー (lib/commands.py) で、ここは押す前に理由を出すだけ。
  // **フェーズでは塞がない** — 調整は準備中に、シーケンスからの退避は試合中に要る。
  // **モード切替と手動指令は別の理由で塞がる** (docs/invariants.md 「手動はフェーズで
  // ゲートしないが、緊急停止ゲートは別軸で効く」)。1 つにまとめると、停止中に手動へ
  // 寄せて解除と同時に動かす手順が取れなくなる
  const modeBlockedReason = connected ? null : "切断中のため切り替えできません";
  const manualBlockedReason = !connected
    ? "切断中のため操作できません"
    : eStopActive
      ? "緊急停止中は手動操縦できません"
      : null;

  /**
   * START が「先頭へ戻して全工程を走り直す」意味になっているか。
   *
   * `sequence_stop` は `step_index` を保持したまま降りるので、画面は中断位置を出したまま
   * START を差し出し、押すと中断姿勢のまま先頭の動作が走る。**Space も同じ経路を通す**
   * (キー 1 打で全工程が走り出す方が、ボタンより危ない)。
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

  // Space に主操作を集約する (トリガー待ちなら NEXT、待機中なら START)。ルーターは
  // 表示中のタブしか描画しないので、表示中のロボットにだけ届く。**手動モード中は
  // 無効化する** —— 誤爆した Space が sequence_start になると、手動で機構を動かして
  // いる最中にシーケンスが走り出す
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
  // 動かせるか」は、準備中も試合中も同じ場所で読めなければならない。
  //
  // 総ステップ数を準備中にしか渡さないのは、試合中は `ActionPanel` が `1/22` の
  // 形で同じ数を出しているため (同じ事実を 2 度描かない)
  const modeSwitch = (
    <ModeSwitch
      mode={manual.mode}
      onChange={handleMode}
      blockedReason={modeBlockedReason}
      sequenceName={state.sequence}
      totalSteps={setupPhase ? state.total_steps : null}
    />
  );

  // 手動の操作面。半自動側の主役 (動作確認 / ActionPanel) と同じ列を占める
  const manualPanel = (
    <ManualPanel
      robotKey={robotKey}
      manual={manual}
      blockedReason={manualBlockedReason}
      sendOrReport={sendOrReport}
    />
  );

  /**
   * 機体状態のパネル。**既定の開閉だけが役割で違う** —— 準備中は配線確認が目的なので
   * 開いた状態から始め、試合中は平常時 1 行へ畳む。ただし手動中は畳まない (機体を
   * 直接動かしている最中は、その前提が成り立たない)。
   *
   * `className` に渡してよいのは主軸 (縦) の伸長指定だけ (docs/invariants.md 「grid の
   * 子は既定で縦に伸びる」)。**試合中の右カラムでは、このパネルが縮む側を引き受ける** ——
   * 隣の試合時間は `shrink-0` で潰れないので、強制展開で列の高さを超えたぶんはここが
   * 吸って内部のスクロール (モータ一覧) へ落ちる。**`flex-1` は付けない**: 中身が数行
   * しかない平常時に全高の白い箱になる。
   */
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

  // --- セッティングタイム -------------------------------------------------
  // 指差喚呼と動作確認は Monitor の設定面が持つ (前者は二度読み上げになり、後者は
  // 両ハンドを 1 本で駆動するので機体ごとの入口が意味を持たない)。ここに残るのは
  // 手動操縦と、その手元で見る機体状態。
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

          {/* シーケンス名と総ステップ数はモード帯が持つ。1 行の事実にパネル枠
              1 つぶんの縦を払わない */}
          {subsystemPanel(true, "min-h-0")}
        </div>
      </Page>
    );
  }

  // --- 試合中 / 試合終了 --------------------------------------------------
  // 答えるべき問いは「今 NEXT を押すのか」「押すと何が起きるか」の 2 つだけ。
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
              // 出すのは**塞がれている理由**だけ。操作できるときの案内 (「クリックで
              // 再開」) は、押せば分かることを毎試合読ませるだけの面積になる
              actions={
                stepJumpBlockedReason ? (
                  <span className="text-[0.85em] text-base-content/60">
                    {stepJumpBlockedReason}
                  </span>
                ) : null
              }
            >
              <SequenceStepList
                steps={state.steps ?? []}
                stepIndex={state.step_index}
                waitingTrigger={state.waiting_trigger}
                onJump={handleJump}
                // 可否と、その理由の案内文は同じ `stepJumpBlockedReason` から出す
                // (駆動中に塞ぐ理由・トリガー待ちを塞がない理由はそちらの docstring)
                disabled={stepJumpBlockedReason !== null}
              />
            </Panel>
          </div>
        )}

        {/* 右は参照面。試合時間は操作面へ置かない — 主操作 (ActionPanel) の位置は
            状態によって動かさない約束なので、上に何かを積むと押す前に探し直しになる。
            診断は平常時 1 行に畳み、異常が出たときだけ自分から開く */}
        <div className="flex min-h-0 flex-col gap-2">
          <MatchTimer timer={matchState.timer} />

          {subsystemPanel(inManual, inManual ? "min-h-0 flex-1" : undefined)}
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
