import { EventFeed } from "@/components/EventFeed";
import { MatchSettings, MatchStrip, useMatchConfirm } from "@/components/MatchControl";
import { RobotStatusRow } from "@/components/RobotStatusRow";
import { StartGate } from "@/components/StartGate";
import { SubsystemStatus } from "@/components/SubsystemStatus";
import { Page } from "@/components/ui/Page";
import { Panel } from "@/components/ui/Panel";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { useRobot } from "@/context/RobotContext";
import type { ChecklistRole } from "@/hooks/useRobotSocket";
import { isSetupPhase } from "@/lib/phase";
import { ROBOTS } from "@/lib/robots";

/**
 * Monitor から 2 名の指差喚呼の進み具合を読み取り専用で監視する。
 *
 * 完了済みは件数へ畳み、**残っている項目名だけ**を並べる。Monitor が知りたいのは
 * 「何が残っているか」であって「何が終わったか」ではない。以前は完了・未完を同じ
 * 重さで 16 行並べていたため、開始が遅れている原因を目で差分を取って探す必要があった。
 */
// prop 名を `role` にすると JSX 上で ARIA の role 属性と見分けが付かない
function OperatorProgress({
  checklistRole,
  label,
}: {
  checklistRole: ChecklistRole;
  label: string;
}) {
  const { matchState } = useRobot();
  const checklist = matchState.checklists[checklistRole];
  const items = checklist?.items ?? [];
  const remaining = items.filter((i) => !i.checked);
  const done = checklist?.completed ?? false;

  return (
    <section className="flex min-w-0 shrink-0 flex-col gap-1">
      <div className="flex items-center justify-between gap-2">
        <span className="min-w-0 truncate font-medium">{label}</span>
        <StatusBadge tone={done ? "success" : "warning"}>
          {done ? "完了" : `残り ${remaining.length}`}
        </StatusBadge>
      </div>
      {remaining.length > 0 ? (
        <ul className="flex flex-col pl-1">
          {remaining.map((item) => (
            <li key={item.id} className="truncate text-base-content/80">
              {item.label}
            </li>
          ))}
        </ul>
      ) : null}
      <span className="text-[0.85em] text-base-content/50">
        完了 {items.length - remaining.length} / {items.length}
      </span>
    </section>
  );
}

/**
 * Monitor タブ。フェーズによって役割が変わるため、レイアウトごと切り替える。
 *
 * - セッティングタイム: 問いは 1 つ「試合を開始できるか、できないなら何が足りないか」
 * - 試合中 / 試合終了: 問いは 1 つ「どちらの機体が止まっていて、何か起きていないか」
 */
export function Dashboard() {
  const { states, matchState } = useRobot();
  const { confirmModal, requestConfirm } = useMatchConfirm();

  if (isSetupPhase(matchState.phase)) {
    return (
      <>
        <Page className="grid grid-cols-[minmax(0,1fr)_minmax(20rem,28rem)] grid-rows-[auto_minmax(0,1fr)]">
          {/* 開始可否を画面で最も大きい要素にする。以前これはパネル最下段の
              小さなグレー文字で、何が足りないかは書かれていなかった */}
          <div className="col-span-full">
            <StartGate onStart={() => requestConfirm("start")} />
          </div>

          <div className="flex min-h-0 flex-col gap-2">
            <MatchSettings onRequestConfirm={requestConfirm} />

            {/* 機体の異常は StartGate の警告行にも出るが、どのバス・どのモータかは
                ここを開いて確かめる。準備フェーズはそのための時間なので既定で開く */}
            <Panel legend="機体状態" className="min-h-0 flex-1" bodyClassName="p-1">
              <div className="scroll flex min-h-0 flex-1 flex-col gap-2">
                {ROBOTS.map(({ key, label }) => {
                  const robot = states[key];
                  return (
                    <section key={key} className="flex shrink-0 flex-col">
                      <span className="px-1 font-medium">{label}</span>
                      {robot ? (
                        <SubsystemStatus health={robot.health} motors={robot.motors} defaultOpen />
                      ) : (
                        <span className="px-1 text-base-content/70">データ未受信</span>
                      )}
                    </section>
                  );
                })}
              </div>
            </Panel>
          </div>

          <Panel legend="操縦者の指差喚呼 (読み取り専用)">
            <div className="scroll flex min-h-0 flex-1 flex-col gap-3">
              <OperatorProgress checklistRole="main_hand" label="メインハンド 操縦者" />
              <OperatorProgress checklistRole="sub_hand" label="サブハンド 操縦者" />
            </div>
          </Panel>
        </Page>
        {confirmModal}
      </>
    );
  }

  return (
    <>
      <Page className="grid grid-cols-2 grid-rows-[auto_minmax(0,1fr)_minmax(0,0.42fr)]">
        <div className="col-span-full">
          <MatchStrip onRequestConfirm={requestConfirm} />
        </div>

        {ROBOTS.map(({ key, label }) => (
          <RobotStatusRow key={key} label={label} state={states[key]} />
        ))}

        {/* ヘルス異常はこれまで数秒で消えるトーストにしか出ていなかった。
            Monitor は起きたことを拾う役なので、履歴を画面に残す */}
        <div className="col-span-full min-h-0">
          <Panel legend="イベント" className="h-full" bodyClassName="p-0">
            <EventFeed />
          </Panel>
        </div>
      </Page>
      {confirmModal}
    </>
  );
}
