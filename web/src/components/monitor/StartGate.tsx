import { CircleAlert, Play, TriangleAlert } from "lucide-react";
import { useEffect } from "react";

import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { Panel } from "@/components/ui/Panel";
import { useRobotStates, useRobotStatus } from "@/context/RobotContext";
import { useArmedPress } from "@/hooks/useArmedPress";
import { cx } from "@/lib/cx";
import { evaluateHealth } from "@/lib/healthVerdict";
import { courtLabel, courtTone } from "@/lib/phase";
import { MALFORMED } from "@/lib/protocol";
import { ROBOTS } from "@/lib/robots";
import type { Tone } from "@/lib/tone";
import { TONE_TEXT_CLASS } from "@/lib/tone";

interface Blocker {
  label: string;
  detail: string;
}

interface Warning extends Blocker {
  key: string;
  tone: Tone;
}

const ROLE_LABEL: Record<string, string> = {
  pre_match: "指差喚呼",
};

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

  if (court === null) {
    blockers.push({ label: "コート", detail: "未設定 — 試合準備で赤か青を選んでください" });
  }

  if (!canStart) {
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
    if (incomplete.length === 0) {
      blockers.push({
        label: "指差喚呼",
        detail: checklists === MALFORMED ? "配信を読めていません" : "未完了の項目があります",
      });
    }
  }

  const warnings = ROBOTS.flatMap(({ key, label }): Warning[] => {
    const robot = states[key];
    if (!robot) return [{ key: `${key}:missing`, label, detail: "データ未受信", tone: "error" }];

    const items: Warning[] = [];
    const verdict = evaluateHealth(robot.health, robot.safety, connected);
    if (verdict.tone !== "success") {
      items.push({ key: `${key}:health`, label, detail: verdict.label, tone: verdict.tone });
    }
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
  const hasError = warnings.some((w) => w.tone === "error");
  const accentTone: Tone = !ready
    ? "warning"
    : hasError
      ? "error"
      : warnings.length > 0
        ? "warning"
        : "success";

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
              <span className={cx("font-medium", TONE_TEXT_CLASS[courtTone(court)])}>
                {courtLabel(court)}
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
