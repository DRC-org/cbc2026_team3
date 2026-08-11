import { Button, Modal, ModalBody, ModalFooter, ModalHeader } from "@tsaito18/tuicss-react";
import { useState } from "react";

import { useRobot } from "@/context/RobotContext";
import type { MatchCourt, MatchMode } from "@/hooks/useRobotSocket";

const MODE_OPTIONS: { value: MatchMode; label: string }[] = [
  { value: "semi_auto", label: "半自動 (操縦者 2 名)" },
  { value: "full_auto", label: "全自動" },
];

const COURT_OPTIONS: { value: MatchCourt; label: string; className: string }[] = [
  { value: "red", label: "赤コート", className: "red-255" },
  { value: "blue", label: "青コート", className: "blue-168" },
];

/**
 * Monitor タブの試合制御パネル。モード・コートの切替とフェーズ遷移を担う。
 * 切替はサーバー側でも試合中は拒否されるが、UI 上でも無効化して誤操作を減らす。
 */
export function MatchControl() {
  const { matchState, setMode, setCourt, matchStart, matchFinish, matchReset, connected } =
    useRobot();
  const { mode, court, phase, can_start_match: canStart } = matchState;
  const [confirm, setConfirm] = useState<"start" | "finish" | "reset" | null>(null);

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

  return (
    <>
      <div className="tui-window">
        <fieldset className="tui-fieldset">
          <legend>
            <span className="cyan-168-text">MATCH CONTROL</span>
          </legend>

          <div style={{ display: "flex", flexDirection: "column", gap: "0.75rem" }}>
            <div>
              <div style={{ marginBottom: "0.25rem", opacity: 0.8 }}>MODE</div>
              <div style={{ display: "flex", gap: "0.5rem", flexWrap: "wrap" }}>
                {MODE_OPTIONS.map((opt) => (
                  <Button
                    key={opt.value}
                    className={mode === opt.value ? "cyan-255" : ""}
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

            <div>
              <div style={{ marginBottom: "0.25rem", opacity: 0.8 }}>COURT</div>
              <div style={{ display: "flex", gap: "0.5rem", flexWrap: "wrap" }}>
                {COURT_OPTIONS.map((opt) => (
                  <Button
                    key={opt.value}
                    className={court === opt.value ? opt.className : ""}
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

            {settingsLocked && phase === "match" ? (
              <p style={{ opacity: 0.8 }}>[?] 試合中はモード・コートを変更できません</p>
            ) : (
              <p style={{ opacity: 0.7 }}>[!] 変更するとチェックリストは全てリセットされます</p>
            )}

            <hr />

            <div style={{ display: "flex", gap: "0.5rem", flexWrap: "wrap" }}>
              <Button
                className="green-255"
                disabled={startBlockedReason !== null}
                onClick={() => setConfirm("start")}
                aria-label="試合を開始する"
              >
                ► 試合開始
              </Button>
              <Button
                className="red-255"
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
              <span style={{ opacity: 0.7 }}>[?] 試合開始 不可: {startBlockedReason}</span>
            ) : null}
          </div>
        </fieldset>
      </div>

      <Modal
        open={confirm !== null}
        onClose={() => setConfirm(null)}
        windowClassName="red-168 left-align"
      >
        <ModalHeader>
          {confirm === "start" ? "START MATCH" : confirm === "finish" ? "FINISH MATCH" : "RESET"}
        </ModalHeader>
        <ModalBody>
          {confirm === "start" ? (
            <>
              <p>
                <span className="info-text">
                  {mode === "full_auto" ? "全自動" : "半自動"} / {court === "red" ? "赤" : "青"}
                  コート
                </span>{" "}
                で試合を開始します。
              </p>
              {mode === "full_auto" ? (
                <p className="danger-text" style={{ marginTop: "0.5rem" }}>
                  ⚠ 全自動モードでは開始と同時に両ロボットが動き出します。
                </p>
              ) : (
                <p style={{ marginTop: "0.5rem", opacity: 0.8 }}>
                  半自動モードでは各操縦者が自分のタブで START を押すまで動きません。
                </p>
              )}
              <p style={{ marginTop: "0.25rem", opacity: 0.8 }}>周囲の安全を確認してください。</p>
            </>
          ) : confirm === "finish" ? (
            <>
              <p>試合を終了します。</p>
              <p style={{ marginTop: "0.5rem", opacity: 0.8 }}>
                実行中のシーケンスは通常停止します (緊急停止ではありません)。
              </p>
            </>
          ) : (
            <>
              <p>セッティングタイムに戻します。</p>
              <p style={{ marginTop: "0.5rem", opacity: 0.8 }}>
                チェックリストは全てリセットされ、再度の指差喚呼が必要になります。
              </p>
            </>
          )}
        </ModalBody>
        <ModalFooter>
          <Button onClick={() => setConfirm(null)}>キャンセル</Button>
          <Button className="red-255" onClick={handleConfirm}>
            実行
          </Button>
        </ModalFooter>
      </Modal>
    </>
  );
}
