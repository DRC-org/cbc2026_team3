import { RotateCcw, Square } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { Modal } from "@/components/ui/Modal";
import { useRobotCommands, useRobotStatus } from "@/context/RobotContext";
import { useArmedPress } from "@/hooks/useArmedPress";
import { isDuringMatch } from "@/lib/phase";

/**
 * リセットの確認ダイアログ。
 *
 * 試合の開始・終了は同じボタンの二度押しで確認を取る（`useArmedPress`）。試合中に
 * 押すそれらと違い、リセットは試合と試合の間にしか押さず、しかも指差喚呼を
 * やり直させる破壊的な操作なので、カーソルを運ぶ数百 ms より読ませることを取る。
 *
 * **準備中のやり直しはこれ 1 つに寄せてある。** かつて `MatchPrep` のヘッダには
 * 指差喚呼のチェックだけを外す CLEAR（`checklist_reset`）が別にあったが、準備フェーズでは
 * フェーズもタイマーも既に初期状態なので `match_reset` と結果が変わらず、**結果が同じ
 * ボタンが 2 つ**並んでいた。どちらを押すべきかは画面から判断できず、片方だけ確認を
 * 挟むという食い違いも生まれる。
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
 * ヘッダーが常時チップで出しているので、ここで並べると同じ事実が同じ画面に 2 度出る。
 *
 * 終了の確認は同じボタンの二度押しで取る。ダイアログ本文が持っていた
 * 「緊急停止ではない」ことは、武装中にボタンの右隣へ出す。
 *
 * **セッティングへ戻る操作に確認は挟まない。** 試合が終わった後の唯一の進み先であり、
 * 失うのは消化済みのチェックリストだけで、機体は動かない。次の試合の準備を
 * 1 クリック遅らせる理由がない（同じ `match_reset` でも、準備中に押す
 * `MatchPrep` ヘッダーの RESET はまだ使っていない指差喚呼を捨てるので確認を残してある）。
 *
 * **EMG STOP の真下に押下可能な要素を置かない。** この帯はヘッダー直下の最上段に出るので、
 * 右端へ寄せると操作ボタンが EMG STOP のほぼ真下（右 16px・下 12px）に来る。誤爆の向きは
 * 「この帯のボタンを狙って外し、緊急停止を踏む」で、試合中に起きればシーケンスが止まる。
 * 操作は帯の先頭へ置き、右端には何も置かない。
 *
 * **ボタンは帯の先頭に固定する。** 試合終了とセッティングへ戻るは同じ場所へ交互に出る
 * ものなので、フェーズで位置が変わると押す直前に探し直すことになる。同じ理由で武装中の
 * 説明文はボタンの右へ出し、幅は `w-[11em]` で固定する —— 説明が左にあると押した瞬間に
 * ボタンが横へずれ、二度押しの 2 回目が 1 回目と違う場所になる。
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
