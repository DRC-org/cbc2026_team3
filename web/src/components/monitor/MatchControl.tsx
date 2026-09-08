import { RotateCcw, Square } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { Modal } from "@/components/ui/Modal";
import { useRobotCommands, useRobotStatus } from "@/context/RobotContext";
import { useArmedPress } from "@/hooks/useArmedPress";
import { isDuringMatch } from "@/lib/phase";

/**
 * リセットの確認ダイアログ。試合の開始・終了と違い、リセットは試合と試合の間にしか
 * 押さず、しかも指差喚呼をやり直させる破壊的な操作なので、カーソルを運ぶ数百 ms より
 * 読ませることを取る（docs/invariants.md 「同じ `match_reset` でも、確認の要否は
 * 『何を失うか』で決める」）。**準備中のやり直しはこれ 1 つに寄せてある。**
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
 * この導線を隠すと試合を終われなくなる。**残すのは導線だけ** — フェーズとコートは
 * ヘッダーが常時チップで出しているので、ここで並べると同じ事実が 2 度出る。
 *
 * 終了の確認は同じボタンの二度押しで取り、ダイアログ本文が持っていた「緊急停止では
 * ない」ことは武装中にボタンの右隣へ出す。**セッティングへ戻る操作に確認は挟まない**
 * （docs/invariants.md 「同じ `match_reset` でも、確認の要否は『何を失うか』で決める」）。
 *
 * **ボタンは帯の先頭に固定する**（docs/invariants.md 「EMG STOP の周囲に押下可能な
 * 要素を置かない」）。この帯はヘッダー直下の最上段なので、右端へ寄せると操作ボタンが
 * EMG STOP のほぼ真下（右 16px・下 12px）に来る。フェーズで位置が変わらないことにも
 * 意味があり、武装中の説明文をボタンの右へ出して幅を `w-[11em]` に固定するのは、
 * 説明が左にあると押した瞬間にボタンが横へずれて 2 回目が別の場所になるため。
 */
export function MatchStrip() {
  const { matchState, connected } = useRobotStatus();
  const { matchFinish, matchReset } = useRobotCommands();
  const { phase } = matchState;
  const duringMatch = isDuringMatch(phase);
  const { armed, press, disarm } = useArmedPress(matchFinish);

  // 試合が終わった後まで武装を持ち越さない（ボタン自体が別物へ入れ替わる）。
  // **切断でも解く。** 武装は押した瞬間の状況に紐づいており、届かなかった 1 回目を
  // 復帰後の 1 回目と繋げると、確認なしで match_finish が飛ぶ（StartGate は
  // 最初から connected を武装解除の条件に含めている）
  useEffect(() => {
    if (!duringMatch || !connected) disarm();
  }, [duringMatch, connected, disarm]);

  return (
    <div className="flex shrink-0 items-center gap-2 border border-base-300 bg-base-100 px-2 py-1">
      {duringMatch ? (
        <>
          {/* 二度押しで文言が「試合終了」→「もう一度押して終了」と伸びても
              ボタンの幅と位置を動かさない（2 回目を 1 回目と同じ場所で受ける） */}
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
    </div>
  );
}
