import { useState } from "react";

import { Button } from "@/components/ui/Button";
import type { ButtonTone } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";
import { Panel } from "@/components/ui/Panel";
import { useRobot } from "@/context/RobotContext";
import type { MatchCourt, MatchMode } from "@/hooks/useRobotSocket";
import { COURT_LABEL, MODE_LABEL } from "@/lib/phase";

const MODE_OPTIONS: { value: MatchMode; label: string }[] = [
  { value: "semi_auto", label: "半自動 (操縦者 2 名)" },
  { value: "full_auto", label: "全自動" },
];

const COURT_OPTIONS: { value: MatchCourt; label: string; tone: ButtonTone }[] = [
  { value: "red", label: "赤コート", tone: "danger" },
  { value: "blue", label: "青コート", tone: "info" },
];

type ConfirmKind = "start" | "finish" | "reset";

interface MatchControlProps {
  /**
   * full   — セッティングタイム用。モード / コート / フェーズ遷移を全て扱う
   * compact — 試合中・試合終了後用。画面をロボット状態に明け渡しつつ、
   *           試合を終える導線 (match_finish / match_reset) だけを 1 行で残す
   */
  variant?: "full" | "compact";
}

/**
 * 試合制御パネル。モード・コートの切替とフェーズ遷移を担う。
 * 切替はサーバー側でも試合中は拒否されるが、UI 上でも無効化して誤操作を減らす。
 */
export function MatchControl({ variant = "full" }: MatchControlProps) {
  const { matchState, setMode, setCourt, matchStart, matchFinish, matchReset, connected } =
    useRobot();
  const { mode, court, phase, can_start_match: canStart } = matchState;
  const [confirm, setConfirm] = useState<ConfirmKind | null>(null);

  const settingsLocked = phase === "match" || !connected;

  const startBlockedReason = !connected
    ? "切断中"
    : phase === "match"
      ? "すでに試合中"
      : phase === "finished"
        ? "リセットしてください"
        : !canStart
          ? "チェックリスト未完了"
          : null;

  const handleConfirm = () => {
    if (confirm === "start") matchStart();
    if (confirm === "finish") matchFinish();
    if (confirm === "reset") matchReset();
    setConfirm(null);
  };

  const confirmModal = (
    <Modal
      open={confirm !== null}
      onClose={() => setConfirm(null)}
      tone="danger"
      title={confirm === "start" ? "START MATCH" : confirm === "finish" ? "FINISH MATCH" : "RESET"}
      footer={
        <>
          <Button onClick={() => setConfirm(null)}>キャンセル</Button>
          <Button tone="danger" onClick={handleConfirm}>
            実行
          </Button>
        </>
      }
    >
      {confirm === "start" ? (
        <>
          <p>
            <span className="text-info">
              {MODE_LABEL[mode]} / {COURT_LABEL[court]}
            </span>{" "}
            で試合を開始します。
          </p>
          {mode === "full_auto" ? (
            <p className="mt-2 text-error">
              ⚠ 全自動モードでは開始と同時に両ロボットが動き出します。
            </p>
          ) : (
            <p className="mt-2 text-fg-dim">
              半自動モードでは各操縦者が自分のタブで START を押すまで動きません。
            </p>
          )}
          <p className="mt-1 text-fg-dim">周囲の安全を確認してください。</p>
        </>
      ) : confirm === "finish" ? (
        <>
          <p>試合を終了します。</p>
          <p className="mt-2 text-fg-dim">
            実行中のシーケンスは通常停止します (緊急停止ではありません)。
          </p>
        </>
      ) : (
        <>
          <p>セッティングタイムに戻します。</p>
          <p className="mt-2 text-fg-dim">
            チェックリストは全てリセットされ、再度の指差喚呼が必要になります。
          </p>
        </>
      )}
    </Modal>
  );

  if (variant === "compact") {
    return (
      <>
        <Panel legend="MATCH" className="shrink-0">
          <div className="hstack">
            <span className="min-w-0 flex-1 truncate">
              {MODE_LABEL[mode]} / {COURT_LABEL[court]}
            </span>
            {phase === "match" ? (
              <Button
                tone="danger"
                onClick={() => setConfirm("finish")}
                aria-label="試合を終了する"
              >
                ■ 試合終了
              </Button>
            ) : (
              <Button
                tone="warn"
                onClick={() => setConfirm("reset")}
                aria-label="セッティングタイムへ戻す"
              >
                ↺ セッティングへ戻る
              </Button>
            )}
          </div>
        </Panel>
        {confirmModal}
      </>
    );
  }

  return (
    <>
      <Panel legend="MATCH CONTROL">
        <div className="group">
          <div className="group-title">MODE</div>
          <div className="hstack flex-wrap">
            {MODE_OPTIONS.map((opt) => (
              <Button
                key={opt.value}
                selected={mode === opt.value}
                disabled={settingsLocked}
                onClick={() => setMode(opt.value)}
                aria-pressed={mode === opt.value}
              >
                {mode === opt.value ? "◆ " : "◇ "}
                {opt.label}
              </Button>
            ))}
          </div>
        </div>

        <div className="group">
          <div className="group-title">COURT</div>
          <div className="hstack flex-wrap">
            {COURT_OPTIONS.map((opt) => (
              <Button
                key={opt.value}
                tone={court === opt.value ? opt.tone : "default"}
                selected={court === opt.value}
                disabled={settingsLocked}
                onClick={() => setCourt(opt.value)}
                aria-pressed={court === opt.value}
              >
                {court === opt.value ? "◆ " : "◇ "}
                {opt.label}
              </Button>
            ))}
          </div>
        </div>

        <p className="mt-2 text-fg-dim">
          {settingsLocked && phase === "match"
            ? "[?] 試合中はモード・コートを変更できません"
            : "[!] 変更するとチェックリストは全てリセットされます"}
        </p>

        <div className="group">
          <div className="hstack flex-wrap">
            <Button
              // 押せない状態で強調色のままだと「押せそうなのに反応しない」と読める
              tone={startBlockedReason === null ? "ok" : "default"}
              disabled={startBlockedReason !== null}
              onClick={() => setConfirm("start")}
              aria-label="試合を開始する"
            >
              ► 試合開始
            </Button>
            <Button
              tone="danger"
              disabled={phase !== "match"}
              onClick={() => setConfirm("finish")}
              aria-label="試合を終了する"
            >
              ■ 試合終了
            </Button>
            <Button onClick={() => setConfirm("reset")} aria-label="セッティングタイムへ戻す">
              ↺ リセット
            </Button>
          </div>
          {startBlockedReason ? (
            <span className="text-fg-dim">[?] 試合開始 不可: {startBlockedReason}</span>
          ) : null}
        </div>
      </Panel>

      {confirmModal}
    </>
  );
}
