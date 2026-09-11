import { RotateCcw, Square } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { Modal } from "@/components/ui/Modal";
import { useRobotCommands, useRobotStatus } from "@/context/RobotContext";
import { useArmedPress } from "@/hooks/useArmedPress";
import { useRemainingMs } from "@/hooks/useRemainingMs";
import { isDuringMatch } from "@/lib/phase";
import { formatRemaining } from "@/lib/time";

export function useResetConfirm() {
  const { matchReset } = useRobotCommands();
  const [open, setOpen] = useState(false);

  const requestReset = useCallback(() => setOpen(true), []);
  const close = useCallback(() => setOpen(false), []);

  const handleConfirm = () => {
    matchReset();
    setOpen(false);
  };

  const confirmModal = (
    <Modal
      open={open}
      onClose={close}
      tone="danger"
      title="RESET"
      footer={
        <>
          <Button onClick={close}>キャンセル</Button>
          <Button tone="danger" onClick={handleConfirm}>
            実行
          </Button>
        </>
      }
    >
      <p>セッティングタイムに戻します。</p>
      <p className="mt-2 text-base-content/70">
        コートは未設定に戻り、選び直すまで試合を開始できません。
      </p>
    </Modal>
  );

  return { confirmModal, requestReset };
}

export function MatchStrip() {
  const { matchState, connected } = useRobotStatus();
  const { matchFinish, matchReset } = useRobotCommands();
  const { phase } = matchState;
  const duringMatch = isDuringMatch(phase);
  const { armed, press, disarm } = useArmedPress(matchFinish);
  const remainingMs = useRemainingMs(matchState.timer);

  useEffect(() => {
    if (!duringMatch || !connected) disarm();
  }, [duringMatch, connected, disarm]);

  return (
    <div className="flex shrink-0 items-center gap-2 border border-base-300 bg-base-100 px-2 py-1">
      {duringMatch ? (
        <>
          <Button
            tone="danger"
            disabled={!connected}
            onClick={press}
            aria-label={
              !connected
                ? "操作不可: 切断中のため送信できません"
                : armed
                  ? "もう一度押して試合を終了する"
                  : "試合を終了する"
            }
            className="w-[11em] whitespace-nowrap"
          >
            <Icon as={Square} />
            {!connected ? "切断中" : armed ? "もう一度押して終了" : "試合終了"}
          </Button>
          {armed ? (
            <span className="text-base-content/70">
              実行中のシーケンスは通常停止します (緊急停止ではありません)
            </span>
          ) : null}
        </>
      ) : (
        <Button
          tone="warn"
          disabled={!connected}
          onClick={matchReset}
          aria-label={
            !connected ? "操作不可: 切断中のため送信できません" : "セッティングタイムへ戻す"
          }
        >
          <Icon as={RotateCcw} />
          {connected ? "セッティングへ戻る" : "切断中"}
        </Button>
      )}

      {remainingMs === null ? null : (
        <span className="ml-auto flex shrink-0 items-baseline gap-1.5">
          <span className="text-[0.8em] text-base-content/60">
            {duringMatch ? "残り" : "終了時点"}
          </span>
          <span className="font-mono text-[1.9em] leading-none font-bold tabular-nums">
            {formatRemaining(remainingMs)}
          </span>
        </span>
      )}
    </div>
  );
}
