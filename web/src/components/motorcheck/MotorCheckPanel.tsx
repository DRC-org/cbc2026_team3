import { Check, ChevronDown, ChevronRight, ListMinus, Square, TriangleAlert } from "lucide-react";
import { useId, useState } from "react";

import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { useRobotStatus } from "@/context/RobotContext";
import { useMotorCheck } from "@/hooks/useMotorCheck";
import { cx } from "@/lib/cx";
import { motorCheckStatus } from "@/lib/motorCheckStatus";
import { MALFORMED } from "@/lib/protocol";
import { TONE_PROGRESS_CLASS } from "@/lib/tone";

/**
 * 統合動作確認の進捗パネル。**両ハンドで 1 つ**なので robot を取らない。
 *
 * **モーダルにしてはならない。** かつてはモーダルで、しかも起動と同時に自動で
 * 開いていた。`.modal` は全画面の fixed オーバーレイなので、**駆動しているあいだ
 * ずっとヘッダーの EMG STOP がクリックできず**（クリックは背景として吸われ、
 * パネルが閉じるだけ）、`ModalProvider` の `openCount` でホットキーまで封じられて
 * いた。全アクチュエータが順に動いている最中に止める手段だけが画面から消える、
 * という最も踏んではならない形で、起動確認ダイアログの「実行中も緊急停止は即時
 * 優先で動作します」という文面もそのあいだ嘘になっていた。ここは操作の隣で開く
 * だけの面にして、画面の他のどこも覆わないこと。
 *
 * 出すのはシーケンスのステップ一覧と、今どこを走っているか。
 * かつてはモータごとの合否表 (期待値 / 観測値) を並べていたが、判定は
 * シーケンスエンジンが担うようになり、失敗はシーケンスが止まる形で現れる
 * (`SequenceTimeoutError` / `AxisSyncError`)。**「合格」の列は無い** —
 * 到達判定を持たない軸 (duty / on_off) にそれを出すと、動いたかどうかを
 * 機械が見ていないのに見たように読めてしまう。
 *
 * **起動ボタンはここに置かない。** 動作確認の入口は `MotorCheckButton` 1 つで、
 * インライン化した今は同じ区分の中に並ぶので、ここにも置くと同じ操作が隣り合って
 * 2 つ並ぶ。状態と可否の理由もそちらが出す。
 */
export function MotorCheckPanel() {
  const { connected } = useRobotStatus();
  const { state, abort } = useMotorCheck();

  // 完了判定は `lib/motorCheckStatus.ts` の 1 箇所だけが持つ。ここで書き直すと
  // 同じ瞬間にパネルは「完了」、サマリーは「未実行」を出す状態が戻る
  const { outcome, completedSteps: done, failureReason } = motorCheckStatus(state, connected);
  const total = state.total_steps;
  const percent = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0;

  const [manualOpen, setManualOpen] = useState(false);
  // 開閉ボタンと開閉対象を結ぶ。aria-expanded だけでは「何が開くのか」が伝わらない
  const detailsId = useId();
  // 実行中と失敗時は操縦者の開閉操作より優先して開く。畳んだまま見逃させない
  // (`SubsystemStatus` と同じ方針)。畳んだ状態で機体だけが動く画面を作らない
  const forcedOpen = outcome === "running" || outcome === "failed";
  const open = forcedOpen || manualOpen;

  return (
    <div className="flex flex-col">
      <div className="flex items-center gap-2">
        <button
          type="button"
          // 記録するのは「今の見え方の逆」。強制開示中に (v) => !v で反転させると、
          // 見た目が開いたままなのに内部だけ「開く」へ倒れ、実行が終わった後も
          // ステップ一覧が指差喚呼の上に居座り続ける
          onClick={() => setManualOpen(!open)}
          aria-expanded={open}
          aria-controls={detailsId}
          className="flex min-w-0 flex-1 cursor-pointer items-center gap-1.5 py-0.5 text-left text-base-content/70 hover:text-base-content"
        >
          <Icon as={open ? ChevronDown : ChevronRight} className="text-base-content/60" />
          <span className="min-w-0 truncate">手順と結果</span>
        </button>
        {/* 中断は開閉の外側に置く。止める操作を折りたたみの内側へ入れない */}
        {state.running ? (
          <Button tone="danger" onClick={abort}>
            <Icon as={Square} />
            中断
          </Button>
        ) : null}
      </div>

      {open ? (
        <div id={detailsId} className="flex flex-col gap-2 pt-1">
          {state.running ? (
            <div className="flex flex-col gap-1">
              <div className="flex items-center justify-between gap-2">
                <span className="font-mono text-base-content/70 tabular-nums">
                  {done} / {total}
                </span>
                {/* ステップ一覧のハイライトと同じ事実だが、一覧は 15 行あって
                    スクロールで視野から外れる。今動いているものはここに留める */}
                <span className="min-w-0 truncate text-info">{state.current_step ?? "—"}</span>
              </div>
              <progress
                className={cx(
                  "progress h-[0.7rem] w-full border border-base-300 bg-base-200",
                  TONE_PROGRESS_CLASS.info,
                )}
                value={percent}
                max={100}
              />
            </div>
          ) : null}

          {/* 失敗理由はサーバーが `error` / `last_error` の 2 欄で言ってくるので、
              `motorCheckStatus` が畳んだ 1 つだけを出す (両方出すと同じ 1 行が 2 度並ぶ)。
              **全文を出すのはここだけ。** 区分見出しの `MotorCheckSummary` は状態チップ
              しか出さない (同じ理由が truncate 版と並んで 2 度読まれるのを避ける) */}
          {failureReason ? (
            <div className="text-error">
              <p className="flex items-center gap-1.5 font-medium">
                <Icon as={TriangleAlert} />
                動作確認は完了していません
              </p>
              <p className="mt-1">{failureReason}</p>
            </div>
          ) : null}

          {/* **除外は必ず出す。** 出さないと、サブハンド不在でステップが減っているのか、
              本番構成なのに config の書き忘れで減っているのかを操縦者が区別できない
              (どちらも「全ステップ成功」として同じに見える)。**内訳を出すのはここだけ** ——
              区分見出しの `MotorCheckSummary` は件数 1 語しか出さない (畳んでいるあいだも
              「除外がある」ことだけは見えている必要があるため、そちらは残してある) */}
          {state.excluded_steps === MALFORMED ? (
            <div className="text-warning">
              <p className="flex items-center gap-1.5 font-medium">
                <Icon as={TriangleAlert} />
                除外ステップを読み取れませんでした
              </p>
              <p className="mt-1">
                ステップ一覧が全てを表しているとは限りません (配信の形が読めていません)。
              </p>
            </div>
          ) : state.excluded_steps.length > 0 ? (
            <div className="rounded-sm border border-warning/40 bg-warning/10 px-3 py-2">
              <p className="flex items-center gap-1.5 font-medium text-warning">
                <Icon as={ListMinus} />
                この構成に無い軸のステップを {state.excluded_steps.length} 件除外しています
              </p>
              <ul className="mt-1 flex flex-col gap-0.5">
                {state.excluded_steps.map((excluded) => (
                  <li key={excluded.step} className="flex flex-wrap items-baseline gap-x-2">
                    <span className="text-base-content/80">{excluded.step}</span>
                    <span className="font-mono text-[0.85em] text-base-content/60">
                      軸が無い: {excluded.missing_axes.join(", ")}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          ) : null}

          {state.steps.length === 0 ? (
            <p className="px-1 py-1 text-base-content/70">
              {state.available
                ? "動作確認のステップが読み込まれていません。"
                : "この構成では動作確認を実行できません (位置定数が揃っていません)。"}
            </p>
          ) : (
            <ol className="flex flex-col">
              {state.steps.map((step) => {
                const isCurrent = state.running && step.index === state.step_index;
                const isDone = step.index < done;
                return (
                  <li
                    key={step.index}
                    className={cx(
                      "flex items-center gap-2 border-l-2 border-transparent px-2 py-[0.35rem]",
                      isCurrent && "border-l-info bg-base-200 font-medium",
                      isDone && "text-base-content/45",
                    )}
                  >
                    <span className="w-6 shrink-0 text-right font-mono text-base-content/50 tabular-nums">
                      {step.index + 1}
                    </span>
                    <span className="min-w-0 flex-1 truncate">{step.label}</span>
                    {isCurrent ? (
                      <StatusBadge tone="info">実行中</StatusBadge>
                    ) : isDone ? (
                      <Icon as={Check} className="shrink-0 text-success" />
                    ) : null}
                  </li>
                );
              })}
            </ol>
          )}
        </div>
      ) : null}
    </div>
  );
}
