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

export function Dashboard() {
  const states = useRobotStates();
  const { matchState, serverInfo, connected } = useRobotStatus();
  const { matchStart } = useRobotCommands();
  const { confirmModal, requestReset } = useResetConfirm();
  const tempThresholds = tempThresholdsOf(serverInfo);

  if (isSetupPhase(matchState.phase)) {
    return (
      <>
        <Page className="grid grid-cols-[minmax(0,1fr)_minmax(20rem,28rem)] grid-rows-[auto_minmax(0,1fr)]">
          <div className="col-span-full">
            <StartGate onStart={matchStart} />
          </div>

          <MatchPrep onRequestReset={requestReset} />

          <Panel legend="機体状態" className="min-h-0" bodyClassName="p-1">
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
                        sensors={robot.sensors}
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

      <div className="col-span-full min-h-0">
        <Panel legend="イベント" className="h-full" bodyClassName="p-0">
          <EventFeed />
        </Panel>
      </div>
    </Page>
  );
}
