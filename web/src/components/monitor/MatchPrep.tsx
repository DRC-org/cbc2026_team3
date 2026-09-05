import { Check, Info, RotateCcw, Zap } from "lucide-react";
import { memo } from "react";

import { ChecklistItems } from "@/components/monitor/ChecklistItems";
import { MotorCheckButton } from "@/components/motorcheck/MotorCheckButton";
import { MotorCheckPanel } from "@/components/motorcheck/MotorCheckPanel";
import { MotorCheckSummary } from "@/components/motorcheck/MotorCheckSummary";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { Panel } from "@/components/ui/Panel";
import { Section } from "@/components/ui/Section";
import { useRobotCommands, useRobotStatus } from "@/context/RobotContext";
import type { ChecklistGroup } from "@/lib/checklistGroups";
import {
  CHECKLIST_GROUP_TITLE,
  groupChecklistItems,
  nextChecklistItemId,
} from "@/lib/checklistGroups";
import { cx } from "@/lib/cx";
import { isDuringMatch, isSetupPhase } from "@/lib/phase";
import type { ChecklistItem, MatchCourt } from "@/lib/protocol";
import { CHECKLIST_ROLE, MALFORMED } from "@/lib/protocol";
import { TONE_PROGRESS_CLASS } from "@/lib/tone";

const TITLE = "試合準備";

/** 選択中のコートは面で塗る。誤ったコート設定は試合をそのまま落とすため */
const COURT_OPTIONS: { value: MatchCourt; label: string; selectedClass: string }[] = [
  { value: "red", label: "赤コート", selectedClass: "border-error bg-error text-error-content" },
  { value: "blue", label: "青コート", selectedClass: "border-info bg-info text-info-content" },
];

/** 区分ごとの進捗。残っている区分だけが目に入るよう、済んだ区分は主張しない */
function GroupProgress({ items }: { items: readonly ChecklistItem[] }) {
  const done = items.filter((i) => i.checked).length;
  if (items.length === 0) return null;
  return (
    <span
      className={cx(
        "font-mono tabular-nums",
        done === items.length ? "text-success" : "text-base-content/70",
      )}
    >
      {done}/{items.length}
    </span>
  );
}

/**
 * セッティングタイムの左カラム。**操作とその確認を同じ場所に置く**ための面。
 *
 * 以前は指差喚呼 22 項目が 1 本のリストで左に、コート設定と動作確認の操作が右に
 * 分かれていた。操縦者はコートを押してから右のリストの中ほどにある
 * 「コート設定と実配置の一致確認」を探し、動作確認を回してからまた別の位置にある
 * 12 項目を探す、という往復を項目ごとに繰り返していた。ここでは各操作の直下に
 * その操作を確認する項目を置く。どの項目がどこへ行くかは `config/checklist.yaml` の
 * `group` が決め、対応表は `lib/checklistGroups.ts` にある。
 *
 * **全体の進捗は上端に 1 つだけ置く。** 試合開始のゲートは区分ごとではなく全項目の
 * 完了 (`can_start_match`) なので、区分ごとの進捗を足し算させてはならない。
 *
 * **チェックリストが読めなくても操作は残す。** 配信が壊れているときに
 * コート設定や動作確認まで消すと、直す手段ごと画面から無くなる。
 *
 * memo なのは親の都合。Dashboard はテレメトリ (20Hz) を読むため毎秒 40 回
 * 再描画されるが、ここが読むのは試合状態と動作確認の状態だけ。props を足すときは
 * 呼び出し側が毎描画 新しい関数を渡さないよう `useCallback` で安定させること。
 */
export const MatchPrep = memo(function MatchPrep({
  onRequestReset,
}: {
  onRequestReset: () => void;
}) {
  const { matchState, serverInfo, connected } = useRobotStatus();
  const { setChecklistItem, checkAllChecklist, setCourt } = useRobotCommands();
  const { court, phase } = matchState;

  // 読めない配信を空へ倒さない。空は「config に項目が無い」の表現として既に使っており、
  // 混ぜると操縦者は config/checklist.yaml を疑って探しに行く
  const checklists = matchState.checklists;
  const unreadable = checklists === MALFORMED;
  const checklist = unreadable ? undefined : checklists[CHECKLIST_ROLE];
  const items = checklist?.items ?? [];

  // 指差喚呼を触れるのは準備フェーズだけ (サーバー PHASES_PREPARATION と対応)。
  // **切断中も塞ぐ。** チェック状態はサーバー配信が唯一の出どころなので、
  // 押せるままにすると「チェックが付かないだけで理由も出ない」になる —— 同じ画面の
  // コート選択は既に connected を見ており、StartGate も「通信 — サーバーに
  // 接続できていません」を出している。ここだけが黙っていた
  const locked = !isSetupPhase(phase) || !connected;
  // コート変更はサーバーも試合中だけ拒む (PHASES_OUTSIDE_MATCH)
  const courtLocked = isDuringMatch(phase) || !connected;

  const checkedCount = items.filter((i) => i.checked).length;
  const completed = checklist?.completed ?? false;
  const percent = items.length > 0 ? (checkedCount / items.length) * 100 : 0;
  const nextId = nextChecklistItemId(items);
  const grouped = groupChecklistItems(items);

  const itemsOf = (group: ChecklistGroup) => (
    <ChecklistItems
      items={grouped[group]}
      nextId={nextId}
      locked={locked}
      onToggle={(itemId, checked) => setChecklistItem(CHECKLIST_ROLE, itemId, checked)}
      className="-mx-2"
    />
  );

  return (
    <Panel
      legend={TITLE}
      className="min-h-0"
      bodyClassName="p-0"
      actions={
        <>
          {/* 開発用。サーバーが --dev-tools で起動したときだけ出る。
              試合運用では指差喚呼そのものが試合開始のゲートなので、
              押せる状態のまま会場へ持ち込まないよう見た目でも区別する */}
          {serverInfo.dev_tools ? (
            <Button
              tone="warn"
              disabled={locked || completed}
              onClick={() => checkAllChecklist(CHECKLIST_ROLE)}
              aria-label="指差喚呼を開発用に全てチェック"
            >
              <Icon as={Zap} />
              DEV 全チェック
            </Button>
          ) : null}
          {/* **やり直しの導線はこの 1 つだけ。** かつてここに CLEAR (checklist_reset)
              があり、最下段に match_reset のボタンが別にあった。準備フェーズでは
              フェーズもタイマーも既に初期状態なので**結果が同じ 2 つのボタン**になり、
              操縦者はどちらを押すべきか画面から判断できなかった。
              配信を読めていない間は「済んだ項目が 0 件」に見えるので、
              そのときだけは件数で殺さない (直す手段まで消さない) */}
          <Button
            disabled={locked || (!unreadable && checkedCount === 0)}
            onClick={onRequestReset}
            aria-label="指差喚呼をリセットしてセッティングタイムへ戻す"
          >
            <Icon as={RotateCcw} />
            RESET
          </Button>
        </>
      }
    >
      {/* 件数と進捗バーで「あと何項目か」を数えずに読ませる。区分ごとに分けた後も、
          試合開始のゲートは全項目の完了なので合計はここ 1 箇所に出す */}
      <div className="flex shrink-0 items-center gap-3 border-b border-base-300 px-2 py-1">
        <span className="font-mono text-[1.3em] tabular-nums">
          {checkedCount}
          <span className="text-base-content/45">/{items.length}</span>
        </span>
        <progress
          className={cx(
            "progress h-[0.5rem] flex-1 rounded-none bg-base-200",
            completed ? TONE_PROGRESS_CLASS.success : TONE_PROGRESS_CLASS.warning,
          )}
          value={percent}
          max={100}
        />
        {completed ? (
          <span className="flex shrink-0 items-center gap-1 font-medium text-success">
            <Icon as={Check} />
            完了
          </span>
        ) : (
          <span className="shrink-0 text-base-content/70">残り {items.length - checkedCount}</span>
        )}
      </div>

      <div className="scroll flex min-h-0 flex-1 flex-col gap-1.5 px-2 py-1.5">
        {unreadable ? (
          <p className="text-error">
            指差喚呼の配信を読めていません。進捗を画面から確認できません
            (サーバーのログを確認してください)
          </p>
        ) : items.length === 0 ? (
          <p className="text-base-content/70">チェック項目が未定義です (config/checklist.yaml)</p>
        ) : null}

        {grouped.preflight.length > 0 ? (
          <Section
            title={CHECKLIST_GROUP_TITLE.preflight}
            aside={<GroupProgress items={grouped.preflight} />}
          >
            {itemsOf("preflight")}
          </Section>
        ) : null}

        {/* コート設定。誤ったコートのまま試合に入る事故は致命的なので畳まない */}
        <Section
          title={CHECKLIST_GROUP_TITLE.court}
          aside={<GroupProgress items={grouped.court} />}
        >
          {/* 注意はボタンと同じ行に置く。間に挟むと、コートを押してから確認する
              項目までの距離がそのぶん開く (この面はその距離を詰めるためにある) */}
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <div className="join">
              {COURT_OPTIONS.map((opt) => (
                <Button
                  key={opt.value}
                  className={cx("join-item", court === opt.value && opt.selectedClass)}
                  disabled={courtLocked}
                  onClick={() => setCourt(opt.value)}
                  aria-pressed={court === opt.value}
                >
                  {opt.label}
                </Button>
              ))}
            </div>
            <p className="flex items-center gap-1.5 text-[0.9em] text-base-content/70">
              <Icon as={Info} />
              変更するとチェックリストは全てリセットされます
            </p>
          </div>
          {itemsOf("court")}
        </Section>

        {/* 動作確認は両ハンドで 1 本。操作・進捗・結果・それを確認する指差喚呼が
            この 1 区分に縦に並ぶので、回した操縦者はその場で唱えて潰せる
            (以前は右カラムのボタンを押してから左のリストの中ほどにある 12 項目を
            探しに行っており、進捗と結果はさらにモーダルの中だった) */}
        <Section
          title={CHECKLIST_GROUP_TITLE.motor_check}
          aside={
            <span className="flex items-center gap-2">
              <MotorCheckSummary />
              <GroupProgress items={grouped.motor_check} />
            </span>
          }
        >
          <div className="flex flex-wrap items-center gap-2">
            <MotorCheckButton />
          </div>
          {/* 進捗と結果は**この場で開く**。かつてはモーダルで、駆動しているあいだ
              ずっとヘッダーの EMG STOP を覆っていた (`MotorCheckPanel` の docstring) */}
          <MotorCheckPanel />
          {itemsOf("motor_check")}
        </Section>

        {/* group を書いていない項目・UI が知らない group の項目。**必ず描く** —
            落とすと、指差喚呼が 1 つ足りないまま試合開始のゲートだけが開かない */}
        {grouped.other.length > 0 ? (
          <Section
            title={CHECKLIST_GROUP_TITLE.other}
            aside={<GroupProgress items={grouped.other} />}
          >
            {itemsOf("other")}
          </Section>
        ) : null}

        {grouped.final.length > 0 ? (
          <Section
            title={CHECKLIST_GROUP_TITLE.final}
            aside={<GroupProgress items={grouped.final} />}
          >
            {itemsOf("final")}
          </Section>
        ) : null}
      </div>
    </Panel>
  );
});
