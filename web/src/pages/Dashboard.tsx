import { SubsystemStatus } from "@/components/diagnostics/SubsystemStatus";
import { EventFeed } from "@/components/monitor/EventFeed";
import { MatchStrip, useResetConfirm } from "@/components/monitor/MatchControl";
import { MatchPrep } from "@/components/monitor/MatchPrep";
import { RobotStatusRow } from "@/components/monitor/RobotStatusRow";
import { StartGate } from "@/components/monitor/StartGate";
import { Page } from "@/components/ui/Page";
import { Panel } from "@/components/ui/Panel";
import { useRobotCommands, useRobotStates, useRobotStatus } from "@/context/RobotContext";
import { tempThresholdsOf } from "@/lib/healthVerdict";
import { isSetupPhase } from "@/lib/phase";
import { ROBOTS } from "@/lib/robots";

/**
 * Monitor タブ。フェーズによって役割が変わるため、レイアウトごと切り替える。
 *
 * - セッティングタイム: 問いは 1 つ「試合を開始できるか、できないなら何が足りないか」
 * - 試合中 / 試合終了: 問いは 1 つ「どちらの機体が止まっていて、何か起きていないか」
 */
export function Dashboard() {
  const states = useRobotStates();
  const { matchState, serverInfo, connected } = useRobotStatus();
  const { matchStart } = useRobotCommands();
  const { confirmModal, requestReset } = useResetConfirm();
  // 温度しきい値の正はサーバーの config。表示部品は props で受け取る
  const tempThresholds = tempThresholdsOf(serverInfo);

  if (isSetupPhase(matchState.phase)) {
    return (
      <>
        <Page className="grid grid-cols-[minmax(0,1fr)_minmax(20rem,28rem)] grid-rows-[auto_minmax(0,1fr)]">
          {/* 開始可否を画面で最も大きい要素にする。以前これはパネル最下段の
              小さなグレー文字で、何が足りないかは書かれていなかった。
              「何が足りないか」(指差喚呼の残りと機体の要確認) はここだけが答える */}
          <div className="col-span-full">
            <StartGate onStart={matchStart} />
          </div>

          {/* 左は準備そのもの。コート設定・動作確認の操作と、それを確認する指差喚呼を
              同じ場所に置く (以前は操作が右、確認が左に分かれ、項目ごとに往復していた) */}
          <MatchPrep onRequestReset={requestReset} />

          {/* 右は準備の面が答えられないことだけを持つ参照面。機体状態の判定チップは
              StartGate と重複するので出さない (同じ画面に「要確認 3 件」が 2 回並ばない) */}
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
                        connected={connected}
                        tempThresholds={tempThresholds}
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
        </Page>
        {confirmModal}
      </>
    );
  }

  return (
    <Page className="grid grid-cols-2 grid-rows-[auto_minmax(0,1fr)_minmax(0,0.42fr)]">
      <div className="col-span-full">
        <MatchStrip />
      </div>

      {ROBOTS.map(({ key, label }) => (
        <RobotStatusRow
          key={key}
          label={label}
          state={states[key]}
          connected={connected}
          tempThresholds={tempThresholds}
        />
      ))}

      {/* ヘルス異常はこれまで数秒で消えるトーストにしか出ていなかった。
            Monitor は起きたことを拾う役なので、履歴を画面に残す */}
      <div className="col-span-full min-h-0">
        <Panel legend="イベント" className="h-full" bodyClassName="p-0">
          <EventFeed />
        </Panel>
      </div>
    </Page>
  );
}
