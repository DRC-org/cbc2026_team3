import { Info, RotateCcw, Square } from "lucide-react";
import { useCallback, useState } from "react";

import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { Modal } from "@/components/ui/Modal";
import { Panel } from "@/components/ui/Panel";
import { Section } from "@/components/ui/Section";
import { useRobotCommands, useRobotStatus } from "@/context/RobotContext";
import { cx } from "@/lib/cx";
import { COURT_LABEL, isDuringMatch } from "@/lib/phase";
import type { MatchCourt } from "@/lib/protocol";

/** 選択中のコートは面で塗る。誤ったコート設定は試合をそのまま落とすため */
const COURT_OPTIONS: { value: MatchCourt; label: string; selectedClass: string }[] = [
  { value: "red", label: "赤コート", selectedClass: "border-error bg-error text-error-content" },
  { value: "blue", label: "青コート", selectedClass: "border-info bg-info text-info-content" },
];

export type ConfirmKind = "start" | "finish" | "reset";

/**
 * フェーズ遷移の確認ダイアログ。
 *
 * 開始は StartGate、終了は MatchStrip、リセットは MatchSettings と、
 * 呼び出し元が画面ごとに散る。ダイアログ本体と文言をここに一本化し、
 * 呼び出し側は `requestConfirm(kind)` を叩くだけにする。
 */
export function useMatchConfirm() {
  const { matchState } = useRobotStatus();
  const { matchStart, matchFinish, matchReset } = useRobotCommands();
  const { court } = matchState;
  const [confirm, setConfirm] = useState<ConfirmKind | null>(null);

  const requestConfirm = useCallback((kind: ConfirmKind) => setConfirm(kind), []);
  const close = useCallback(() => setConfirm(null), []);

  const handleConfirm = () => {
    if (confirm === "start") matchStart();
    if (confirm === "finish") matchFinish();
    if (confirm === "reset") matchReset();
    setConfirm(null);
  };

  const confirmModal = (
    <Modal
      open={confirm !== null}
      onClose={close}
      tone="danger"
      title={confirm === "start" ? "START MATCH" : confirm === "finish" ? "FINISH MATCH" : "RESET"}
      footer={
        <>
          <Button onClick={close}>キャンセル</Button>
          <Button tone="danger" onClick={handleConfirm}>
            実行
          </Button>
        </>
      }
    >
      {confirm === "start" ? (
        <>
          <p>
            <span className="font-medium text-info">{COURT_LABEL[court]}</span> で試合を開始します。
          </p>
          <p className="mt-2 text-base-content/70">
            各操縦者が自分のタブで START を押すまで機体は動きません。
          </p>
          <p className="mt-1 text-base-content/70">周囲の安全を確認してください。</p>
        </>
      ) : confirm === "finish" ? (
        <>
          <p>試合を終了します。</p>
          <p className="mt-2 text-base-content/70">
            実行中のシーケンスは通常停止します (緊急停止ではありません)。
          </p>
        </>
      ) : (
        <>
          <p>セッティングタイムに戻します。</p>
          <p className="mt-2 text-base-content/70">
            チェックリストは全てリセットされ、再度の指差喚呼が必要になります。
          </p>
        </>
      )}
    </Modal>
  );

  return { confirmModal, requestConfirm };
}

/**
 * コートの設定。セッティングタイム専用。
 *
 * 試合ごとに一度だけ触る設定なので、開始可否 (StartGate) より下に置く。
 * ただし誤ったコート設定のまま試合に入る事故は致命的なので、畳まずに常時見せる。
 */
export function MatchSettings({
  onRequestConfirm,
}: {
  onRequestConfirm: (kind: ConfirmKind) => void;
}) {
  const { matchState, connected } = useRobotStatus();
  const { setCourt } = useRobotCommands();
  const { court, phase } = matchState;
  const settingsLocked = isDuringMatch(phase) || !connected;

  return (
    <Panel legend="試合設定">
      <Section title="COURT">
        <div className="join">
          {COURT_OPTIONS.map((opt) => (
            <Button
              key={opt.value}
              className={cx("join-item", court === opt.value && opt.selectedClass)}
              disabled={settingsLocked}
              onClick={() => setCourt(opt.value)}
              aria-pressed={court === opt.value}
            >
              {opt.label}
            </Button>
          ))}
        </div>
      </Section>

      <Section>
        <p className="flex items-center gap-1.5 text-base-content/70">
          <Icon as={Info} />
          変更するとチェックリストは全てリセットされます
        </p>
        <div className="mt-1 flex flex-wrap items-center gap-2">
          <Button onClick={() => onRequestConfirm("reset")} aria-label="セッティングタイムへ戻す">
            <Icon as={RotateCcw} />
            チェックリストをリセット
          </Button>
        </div>
      </Section>
    </Panel>
  );
}

/**
 * 試合中・試合終了後の 1 行帯。
 *
 * 試合中は画面をロボット状態に明け渡すが、`match_finish` は MATCH フェーズ限定なので
 * この導線を隠すと試合を終われなくなる。設定値の確認と終了導線だけを 1 行で残す。
 */
export function MatchStrip({
  onRequestConfirm,
}: {
  onRequestConfirm: (kind: ConfirmKind) => void;
}) {
  const { matchState } = useRobotStatus();
  const { court, phase } = matchState;

  return (
    <div className="flex shrink-0 items-center justify-between gap-2 border border-base-300 bg-base-100 px-2 py-1">
      <span className="min-w-0 truncate">
        <span className="text-base-content/70">MATCH </span>
        {COURT_LABEL[court]}
      </span>
      {isDuringMatch(phase) ? (
        <Button
          tone="danger"
          onClick={() => onRequestConfirm("finish")}
          aria-label="試合を終了する"
        >
          <Icon as={Square} />
          試合終了
        </Button>
      ) : (
        <Button
          tone="warn"
          onClick={() => onRequestConfirm("reset")}
          aria-label="セッティングタイムへ戻す"
        >
          <Icon as={RotateCcw} />
          セッティングへ戻る
        </Button>
      )}
    </div>
  );
}
