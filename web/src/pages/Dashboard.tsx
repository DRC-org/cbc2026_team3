import { SubsystemStatus } from "@/components/diagnostics/SubsystemStatus";
import { EventFeed } from "@/components/monitor/EventFeed";
import { MatchSettings, MatchStrip, useMatchConfirm } from "@/components/monitor/MatchControl";
import { RobotStatusRow } from "@/components/monitor/RobotStatusRow";
import { StartGate } from "@/components/monitor/StartGate";
import { Page } from "@/components/ui/Page";
import { Panel } from "@/components/ui/Panel";
import { useRobot } from "@/context/RobotContext";
import { isSetupPhase } from "@/lib/phase";
import { ROBOTS } from "@/lib/robots";

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
        <Page className="grid grid-cols-[minmax(0,1fr)_minmax(18rem,24rem)] grid-rows-[auto_minmax(0,1fr)]">
          {/* 開始可否を画面で最も大きい要素にする。以前これはパネル最下段の
              小さなグレー文字で、何が足りないかは書かれていなかった。
              「何が足りないか」(指差喚呼の残りと機体の要確認) はここだけが答える */}
          <div className="col-span-full">
            <StartGate onStart={() => requestConfirm("start")} />
          </div>

          {/* 下の 2 枠は StartGate が答えられないことだけを持つ。
              左は「どのバス・どのモータか」。判定チップは StartGate と重複するので出さない
              (同じ画面に「要確認 3 件」が 2 回並ぶ状態を作らない) */}
          <Panel legend="機体状態 — どのバス・どのモータか" className="min-h-0" bodyClassName="p-1">
            <div className="scroll flex min-h-0 flex-1 flex-col gap-2">
              {ROBOTS.map(({ key, label }) => {
                const robot = states[key];
                return (
                  <section key={key} className="flex shrink-0 flex-col">
                    <span className="px-1 font-medium">{label}</span>
                    {robot ? (
                      <SubsystemStatus
                        health={robot.health}
                        motors={robot.motors}
                        safety={robot.safety}
                        showVerdict={false}
                      />
                    ) : (
                      <span className="px-1 text-base-content/70">データ未受信</span>
                    )}
                  </section>
                );
              })}
            </div>
          </Panel>

          {/* grid の子は既定で縦に伸びる。内容ぶんの高さに留めるには self-start が要る */}
          <div className="self-start">
            <MatchSettings onRequestConfirm={requestConfirm} />
          </div>
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
