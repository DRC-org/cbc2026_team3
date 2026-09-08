import { CircleAlert, Play, TriangleAlert } from "lucide-react";
import { useEffect } from "react";

import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { Panel } from "@/components/ui/Panel";
import { useRobotStates, useRobotStatus } from "@/context/RobotContext";
import { useArmedPress } from "@/hooks/useArmedPress";
import { cx } from "@/lib/cx";
import { evaluateHealth } from "@/lib/healthVerdict";
import { COURT_LABEL, COURT_TONE } from "@/lib/phase";
import { MALFORMED } from "@/lib/protocol";
import { ROBOTS } from "@/lib/robots";
import type { Tone } from "@/lib/tone";
import { TONE_TEXT_CLASS } from "@/lib/tone";

interface Blocker {
  label: string;
  detail: string;
}

/**
 * 開始を止めない「開始前に見るべきこと」。1 機が 2 件以上出しうるので、
 * 表示キーはラベル (機体名) ではなく発生源ごとに分けて持つ。
 *
 * **`tone` は `evaluateHealth` の判定をそのまま運ぶ。** ここで「要確認」へ丸めると、
 * 同じ無励磁に対してタブの LED は「異常あり」(error)、この画面は「要確認」と
 * 呼ぶ状態になる —— 操縦者は 2 つの画面を見比べてどちらが本当か分からなくなる。
 */
interface Warning extends Blocker {
  key: string;
  tone: Tone;
}

const ROLE_LABEL: Record<string, string> = {
  pre_match: "指差喚呼",
};

/**
 * セッティングタイムの主役。「今すぐ試合を開始できるか、できないなら何が足りないか」
 * だけを、画面で最も大きい要素として答える。
 *
 * 以前この情報は試合制御パネル最下段の小さなグレー文字
 * (`試合開始 不可: チェックリスト未完了`) だった。何が未完了なのかは書かれておらず、
 * Monitor は右側に並ぶ 16 行の項目を目でスキャンして差分を取る必要があった。
 * 開始が遅れている原因を探すのに操縦者へ聞きにいく、という運用がそこから生まれる。
 *
 * 開始の確認は**同じボタンの二度押し**で取る（`useArmedPress`）。確認ダイアログは
 * ボタンから離れた位置に出るため、押す → カーソルを運ぶ → 押す、の往復が要った。
 * ダイアログ本文が持っていた情報（コート・機体が動く条件・周囲の安全確認）は
 * 武装中の説明行へ移してある。落とすと二度押しは単なる連打になる。
 */
export function StartGate({ onStart }: { onStart: () => void }) {
  const { matchState, connected } = useRobotStatus();
  const states = useRobotStates();
  const { phase, court, can_start_match: canStart } = matchState;
  const { armed, press, disarm } = useArmedPress(onStart);

  const blockers: Blocker[] = [];
  if (!connected) {
    blockers.push({ label: "通信", detail: "サーバーに接続できていません" });
  }
  if (phase === "finished") {
    blockers.push({ label: "フェーズ", detail: "リセットしてセッティングへ戻してください" });
  }

  // 開始可否を決めるのはサーバーの can_start_match だけ。ここは「なぜ開始できないか」を
  // 説明するに留める。クライアントでも判定し直すと、サーバーは開始できると言っているのに
  // 画面がボタンを殺す状態が生まれる (実際に、配信ロールが 1 つ増えただけでそうなった)。
  if (!canStart) {
    // **残っている項目名はここに出さない。** 同じ画面の Checklist が全項目を並べ、
    // 未完の先頭を「次」として強調している。ここで先頭項目を繰り返すと、
    // 操縦者は同じ 1 行を 2 箇所で読むことになる (以前この画面には Checklist が
    // 無く、右カラムの 16 行をスキャンさせないために項目名を出していた)。
    const checklists = matchState.checklists;
    const incomplete =
      checklists === MALFORMED ? [] : Object.entries(checklists).filter(([, c]) => !c.completed);
    for (const [role, checklist] of incomplete) {
      const remaining = checklist.items.filter((i) => !i.checked);
      blockers.push({
        label: ROLE_LABEL[role] ?? role,
        detail: remaining.length === 0 ? "未完了" : `残り ${remaining.length} 件`,
      });
    }
    // 理由を 1 つも挙げられないまま押せないボタンだけを見せない。
    // 配信が読めていない場合は「残り何件か」も言えないので、そう書く
    if (incomplete.length === 0) {
      blockers.push({
        label: "指差喚呼",
        detail: checklists === MALFORMED ? "配信を読めていません" : "未完了の項目があります",
      });
    }
  }

  // 機体側の異常は「開始できない」ではなく「開始前に見るべきこと」。サーバーは
  // ハードウェア状態で match_start を止めないので、ここでボタンを殺すと軽微な
  // 警告ひとつで試合そのものを始められなくなる。判断は操縦者に残し、見落としだけ防ぐ
  const warnings = ROBOTS.flatMap(({ key, label }): Warning[] => {
    const robot = states[key];
    // 配信が 1 通も無いのは「軽微な確認事項」ではない。判定できない = 異常側
    if (!robot) return [{ key: `${key}:missing`, label, detail: "データ未受信", tone: "error" }];

    const items: Warning[] = [];
    const verdict = evaluateHealth(robot.health, robot.safety, connected);
    if (verdict.tone !== "success") {
      items.push({ key: `${key}:health`, label, detail: verdict.label, tone: verdict.tone });
    }
    // 手動操縦は健全性ではないので evaluateHealth へは足さない (あちらは
    // `lib/healthVerdict.ts` の 1 箇所だけが持つ機体の健全性判定)。ここへ別項目として
    // 並べるのは、指差喚呼 operation_mode_sequence が読む先がこの行だから。
    //
    // **チェックの後で手動へ戻っても外れる**のが要点。指差喚呼は押した瞬間のラッチで、
    // valves_actuate のように手動操縦を要求する項目が同じリストに居るので、
    // final を全部チェックした後に弁をもう一度確かめて手動のまま戻ると
    // can_start_match は true のままになる。ここは毎描画で評価するのでラッチしない
    if (robot.manual?.mode === "manual") {
      items.push({
        key: `${key}:manual`,
        label,
        detail: "手動操縦中 — 半自動へ戻すまで START が拒否されます",
        tone: "warning",
      });
    }
    return items;
  });

  const ready = canStart && connected && phase !== "finished";
  // 異常が 1 件でもあれば異常側。**件数ではなく重さで決める** —— 手動操縦中 (warning) が
  // 1 件あるだけで帯を赤くすると、本物の異常と同じ色になる
  const hasError = warnings.some((w) => w.tone === "error");
  // 帯の色は TONE_BORDER_L_CLASS が唯一の出どころ (Panel が引く)。ここで
  // `border-l-[0.4rem] border-l-*` を三項で組み立てると、色の規則が 2 つになる
  const accentTone: Tone = !ready
    ? "warning"
    : hasError
      ? "error"
      : warnings.length > 0
        ? "warning"
        : "success";

  // 開始できない状況へ変わったら武装を解く。武装は押した瞬間の状況に紐づいており、
  // 通信が切れた・チェックリストが外れた後の 1 回目を 2 回目として扱ってはならない
  useEffect(() => {
    if (!ready) disarm();
  }, [ready, disarm]);

  return (
    <Panel accentTone={accentTone} className="shrink-0" bodyClassName="p-0">
      <div className="flex flex-wrap items-center gap-3 p-3">
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <span className="text-[1.6em] leading-tight font-semibold">
            {armed
              ? "もう一度押すと開始します"
              : ready
                ? "試合を開始できます"
                : "まだ開始できません"}
          </span>
          {armed ? (
            <span className="text-base-content/70">
              <span className={cx("font-medium", TONE_TEXT_CLASS[COURT_TONE[court]])}>
                {COURT_LABEL[court]}
              </span>{" "}
              で試合を開始します。各操縦者が自分のタブで START
              を押すまで機体は動きません。周囲の安全を確認してください。
            </span>
          ) : ready ? (
            <span className="text-base-content/70">
              {warnings.length === 0
                ? "全ての指差喚呼が完了しています。周囲の安全を確認して開始してください。"
                : hasError
                  ? "指差喚呼は完了していますが、機体に異常があります。"
                  : "指差喚呼は完了していますが、機体に要確認があります。"}
            </span>
          ) : (
            <ul className="flex flex-col gap-[0.15rem]">
              {blockers.map((b) => (
                <li key={b.label} className="flex min-w-0 items-baseline gap-2">
                  <Icon as={CircleAlert} className="translate-y-[0.15em] text-warning" />
                  <span className="shrink-0 font-medium">{b.label}</span>
                  <span className="min-w-0 truncate text-base-content/70">{b.detail}</span>
                </li>
              ))}
            </ul>
          )}

          {/* 機体異常は開始を止めないが、READY の文字で覆い隠してもいけない */}
          {warnings.length > 0 ? (
            <ul className="flex flex-col gap-[0.15rem]">
              {warnings.map((w) => (
                <li key={w.key} className="flex min-w-0 items-baseline gap-2">
                  <Icon
                    as={TriangleAlert}
                    className={cx("translate-y-[0.15em]", TONE_TEXT_CLASS[w.tone])}
                  />
                  <span className="shrink-0 font-medium">{w.label}</span>
                  <span className="min-w-0 truncate text-base-content/70">{w.detail}</span>
                </li>
              ))}
            </ul>
          ) : null}
        </div>

        {/* 二度押しで文言が伸びてもボタンの左端を動かさない。押す位置が 1 回目と
            2 回目でずれると、二度押しの利点（カーソルを動かさない）が消える */}
        <Button
          tone={armed ? "danger" : ready ? "ok" : "default"}
          disabled={!ready}
          onClick={press}
          aria-label={armed ? "もう一度押して試合を開始する" : "試合を開始する"}
          className="h-[3.2rem] w-[14em] shrink-0 px-6 text-[1.2em] whitespace-nowrap"
        >
          <Icon as={Play} />
          {armed ? "もう一度押して開始" : "試合開始"}
        </Button>
      </div>
    </Panel>
  );
}
