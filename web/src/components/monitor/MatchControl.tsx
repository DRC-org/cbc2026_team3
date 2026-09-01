import { RotateCcw, Square } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { Modal } from "@/components/ui/Modal";
import { useRobotCommands, useRobotStatus } from "@/context/RobotContext";
import { useArmedPress } from "@/hooks/useArmedPress";
import { COURT_LABEL, isDuringMatch } from "@/lib/phase";

/**
 * リセットの確認ダイアログ。
 *
 * 試合の開始・終了は同じボタンの二度押しで確認を取る（`useArmedPress`）。試合中に
 * 押すそれらと違い、リセットは試合と試合の間にしか押さず、しかも指差喚呼を
 * やり直させる破壊的な操作なので、カーソルを運ぶ数百 ms より読ませることを取る。
 *
 * 呼び出し元は MatchPrep（準備中）と Dashboard に散るため、文言はここに一本化する。
 */
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
        チェックリストは全てリセットされ、再度の指差喚呼が必要になります。
      </p>
    </Modal>
  );

  return { confirmModal, requestReset };
}

/**
 * 試合中・試合終了後の 1 行帯。
 *
 * 試合中は画面をロボット状態に明け渡すが、`match_finish` は MATCH フェーズ限定なので
 * この導線を隠すと試合を終われなくなる。設定値の確認と終了導線だけを 1 行で残す。
 *
 * 終了の確認は同じボタンの二度押しで取る。ダイアログ本文が持っていた
 * 「緊急停止ではない」ことは、武装中に隣へ出す。
 *
 * **セッティングへ戻る操作に確認は挟まない。** 試合が終わった後の唯一の進み先であり、
 * 失うのは消化済みのチェックリストだけで、機体は動かない。次の試合の準備を
 * 1 クリック遅らせる理由がない（同じ `match_reset` でも、準備中に押す
 * MatchPrep 最下段のリセットはまだ使っていない指差喚呼を捨てるので確認を残してある）。
 */
export function MatchStrip() {
  const { matchState } = useRobotStatus();
  const { matchFinish, matchReset } = useRobotCommands();
  const { court, phase } = matchState;
  const duringMatch = isDuringMatch(phase);
  const { armed, press, disarm } = useArmedPress(matchFinish);

  // 試合が終わった後まで武装を持ち越さない（ボタン自体が別物へ入れ替わる）
  useEffect(() => {
    if (!duringMatch) disarm();
  }, [duringMatch, disarm]);

  return (
    <div className="flex shrink-0 items-center justify-between gap-2 border border-base-300 bg-base-100 px-2 py-1">
      <span className="min-w-0 truncate">
        <span className="text-base-content/70">MATCH </span>
        {COURT_LABEL[court]}
      </span>
      {duringMatch ? (
        <span className="flex items-center gap-2">
          {armed ? (
            <span className="text-base-content/70">
              実行中のシーケンスは通常停止します (緊急停止ではありません)
            </span>
          ) : null}
          {/* 二度押しで文言が伸びてもボタンの左端を動かさない */}
          <Button
            tone="danger"
            onClick={press}
            aria-label={armed ? "もう一度押して試合を終了する" : "試合を終了する"}
            className="w-[11em] whitespace-nowrap"
          >
            <Icon as={Square} />
            {armed ? "もう一度押して終了" : "試合終了"}
          </Button>
        </span>
      ) : (
        <Button tone="warn" onClick={matchReset} aria-label="セッティングタイムへ戻す">
          <Icon as={RotateCcw} />
          セッティングへ戻る
        </Button>
      )}
    </div>
  );
}
