import { CircleAlert, Play, TriangleAlert } from "lucide-react";
import { useEffect } from "react";

import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { useRobotStates, useRobotStatus } from "@/context/RobotContext";
import { useArmedPress } from "@/hooks/useArmedPress";
import { cx } from "@/lib/cx";
import { evaluateHealth } from "@/lib/healthVerdict";
import { COURT_LABEL, COURT_TONE } from "@/lib/phase";
import { ROBOTS } from "@/lib/robots";
import { TONE_TEXT_CLASS } from "@/lib/tone";

interface Blocker {
  label: string;
  detail: string;
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
    const incomplete = Object.entries(matchState.checklists).filter(([, c]) => !c.completed);
    for (const [role, checklist] of incomplete) {
      const remaining = checklist.items.filter((i) => !i.checked);
      blockers.push({
        label: ROLE_LABEL[role] ?? role,
        detail: remaining.length === 0 ? "未完了" : `残り ${remaining.length} 件`,
      });
    }
    // 理由を 1 つも挙げられないまま押せないボタンだけを見せない
    if (incomplete.length === 0) {
      blockers.push({ label: "指差喚呼", detail: "未完了の項目があります" });
    }
  }

  // 機体側の異常は「開始できない」ではなく「開始前に見るべきこと」。サーバーは
  // ハードウェア状態で match_start を止めないので、ここでボタンを殺すと軽微な
  // 警告ひとつで試合そのものを始められなくなる。判断は操縦者に残し、見落としだけ防ぐ
  const warnings = ROBOTS.flatMap(({ key, label }) => {
    const robot = states[key];
    if (!robot) return [{ label, detail: "データ未受信" }];
    const verdict = evaluateHealth(robot.health, robot.motors, robot.safety);
    return verdict.tone === "success" ? [] : [{ label, detail: verdict.label }];
  });

  const ready = canStart && connected && phase !== "finished";

  // 開始できない状況へ変わったら武装を解く。武装は押した瞬間の状況に紐づいており、
  // 通信が切れた・チェックリストが外れた後の 1 回目を 2 回目として扱ってはならない
  useEffect(() => {
    if (!ready) disarm();
  }, [ready, disarm]);

  return (
    <section
      className={cx(
        "card card-border flex shrink-0 flex-col border-base-300 bg-base-100",
        ready && warnings.length === 0 ? "border-l-[0.4rem] border-l-success" : null,
        ready && warnings.length > 0 ? "border-l-[0.4rem] border-l-error" : null,
        !ready ? "border-l-[0.4rem] border-l-warning" : null,
      )}
    >
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
                <li key={w.label} className="flex min-w-0 items-baseline gap-2">
                  <Icon as={TriangleAlert} className="translate-y-[0.15em] text-error" />
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
    </section>
  );
}
