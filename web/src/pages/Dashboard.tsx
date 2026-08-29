import { ListChecks } from "lucide-react";
import { useState } from "react";

import { SubsystemStatus } from "@/components/diagnostics/SubsystemStatus";
import { Checklist } from "@/components/monitor/Checklist";
import { EventFeed } from "@/components/monitor/EventFeed";
import { MatchSettings, MatchStrip, useMatchConfirm } from "@/components/monitor/MatchControl";
import { RobotStatusRow } from "@/components/monitor/RobotStatusRow";
import { StartGate } from "@/components/monitor/StartGate";
import { MotorCheckButton } from "@/components/motorcheck/MotorCheckButton";
import { MotorCheckPanel } from "@/components/motorcheck/MotorCheckPanel";
import { MotorCheckSummary } from "@/components/motorcheck/MotorCheckSummary";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { Page } from "@/components/ui/Page";
import { Panel } from "@/components/ui/Panel";
import { useRobotStates, useRobotStatus } from "@/context/RobotContext";
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
  const { matchState } = useRobotStatus();
  const { confirmModal, requestConfirm } = useMatchConfirm();
  const [checkPanelOpen, setCheckPanelOpen] = useState(false);

  if (isSetupPhase(matchState.phase)) {
    return (
      <>
        <Page className="grid grid-cols-[minmax(0,1fr)_minmax(20rem,28rem)] grid-rows-[auto_minmax(0,1fr)]">
          {/* 開始可否を画面で最も大きい要素にする。以前これはパネル最下段の
              小さなグレー文字で、何が足りないかは書かれていなかった。
              「何が足りないか」(指差喚呼の残りと機体の要確認) はここだけが答える */}
          <div className="col-span-full">
            <StartGate onStart={() => requestConfirm("start")} />
          </div>

          {/* 左は指差喚呼。準備フェーズの作業そのものなので、画面の広い面を割く。
              操縦者 2 名は同じ場所で同じ機体を見るため、ここ 1 つに統合してある
              (かつては各操縦者のタブに 1 つずつ置いて二度読み上げていた) */}
          <Checklist />

          {/* 右は StartGate と指差喚呼が答えられないことだけを持つ参照面 +
              準備フェーズの唯一の操作。機体状態の判定チップは StartGate と
              重複するので出さない (同じ画面に「要確認 3 件」が 2 回並ばない) */}
          <div className="flex min-h-0 flex-col gap-2">
            {/* 動作確認は両ハンドで 1 本。指差喚呼の motor_check 項目と対になるので、
                チェックリストと同じ画面に置く (以前は各操縦者のタブに 1 つずつあり、
                2 つを同時に起動できたため両機が同時に動きうる状態だった) */}
            <Panel
              legend="アクチュエータ動作確認"
              className="shrink-0"
              actions={<MotorCheckSummary />}
            >
              <div className="flex flex-wrap items-center gap-2">
                <MotorCheckButton onPanelOpen={() => setCheckPanelOpen(true)} />
                <Button onClick={() => setCheckPanelOpen(true)}>
                  <Icon as={ListChecks} />
                  進捗を表示
                </Button>
              </div>
            </Panel>

            <Panel
              legend="機体状態 — どのバス・どのモータか"
              className="min-h-0 flex-1"
              bodyClassName="p-1"
            >
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

            <div className="shrink-0">
              <MatchSettings onRequestConfirm={requestConfirm} />
            </div>
          </div>
        </Page>
        {confirmModal}
        <MotorCheckPanel isOpen={checkPanelOpen} onOpenChange={setCheckPanelOpen} />
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
