import { Tab, TabList, TabPanel, Tabs } from "@tsaito18/tuicss-react";
import { useCallback, useEffect } from "react";

import { EStopOverlay } from "@/components/EStopOverlay";
import { PhaseBanner } from "@/components/PhaseBanner";
import { RobotProvider } from "@/context/RobotContext";
import { useRobotSocket } from "@/hooks/useRobotSocket";
import type { ChecklistRole, MatchCourt, MatchMode } from "@/hooks/useRobotSocket";
import { Dashboard } from "@/pages/Dashboard";
import { MotorTuning } from "@/pages/MotorTuning";
import { RobotControl } from "@/pages/RobotControl";

const REJECTION_VISIBLE_MS = 4000;

export function App() {
  const socket = useRobotSocket();
  const { send, rejection, clearRejection } = socket;

  const onEStop = useCallback(() => {
    send({ type: "e_stop" });
    socket.setEStopActive(true);
  }, [send, socket]);

  const onEStopRelease = useCallback(() => {
    send({ type: "e_stop_release" });
    socket.setEStopActive(false);
  }, [send, socket]);

  const setMode = useCallback((mode: MatchMode) => send({ type: "set_mode", mode }), [send]);
  const setCourt = useCallback((court: MatchCourt) => send({ type: "set_court", court }), [send]);
  const setChecklistItem = useCallback(
    (role: ChecklistRole, itemId: string, checked: boolean) =>
      send({ type: "checklist_set", role, item_id: itemId, checked }),
    [send],
  );
  const resetChecklist = useCallback(
    (role: ChecklistRole) => send({ type: "checklist_reset", role }),
    [send],
  );
  const matchStart = useCallback(() => send({ type: "match_start" }), [send]);
  const matchFinish = useCallback(() => send({ type: "match_finish" }), [send]);
  const matchReset = useCallback(() => send({ type: "match_reset" }), [send]);

  // 拒否通知は一定時間で自動的に消す。操作の手を止めさせないため確認ボタンは置かない
  useEffect(() => {
    if (!rejection) return;
    const timer = setTimeout(clearRejection, REJECTION_VISIBLE_MS);
    return () => clearTimeout(timer);
  }, [rejection, clearRejection]);

  return (
    <RobotProvider
      value={{
        states: socket.states,
        connected: socket.connected,
        eStopActive: socket.eStopActive,
        healthEvents: socket.healthEvents,
        motorChecks: socket.motorChecks,
        matchState: socket.matchState,
        rejection: socket.rejection,
        clearRejection,
        send,
        onEStop,
        onEStopRelease,
        setMode,
        setCourt,
        setChecklistItem,
        resetChecklist,
        matchStart,
        matchFinish,
        matchReset,
      }}
    >
      <div className="wrapper white-168">
        <div
          className="tui-panel full-width center cyan-168 black-255-text tui-no-shadow"
          style={{ height: "20px" }}
        >
          cbc2026_team3_controller
        </div>
        <Tabs defaultValue="monitor">
          <TabList>
            <Tab value="monitor">Monitor</Tab>
            <Tab value="main-hand">Main Hand</Tab>
            <Tab value="sub-hand">Sub Hand</Tab>
            <Tab value="pid-tuning">PID Tuning</Tab>
          </TabList>
          <TabPanel value="monitor">
            <Dashboard />
          </TabPanel>
          <TabPanel value="main-hand">
            <RobotControl robotKey="main_hand" label="メインハンド" />
          </TabPanel>
          <TabPanel value="sub-hand">
            <RobotControl robotKey="sub_hand" label="サブハンド" />
          </TabPanel>
          <TabPanel value="pid-tuning">
            <MotorTuning />
          </TabPanel>
        </Tabs>
        <div className="tui-statusbar cyan-168 absolute" style={{ height: "1.5rem" }}>
          <ul>
            <li>
              {socket.connected ? (
                <>
                  <span className="symbol green-255-text">●</span> Connected
                </>
              ) : (
                <>
                  <span className="symbol red-255-text">●</span> Disconnected
                </>
              )}
            </li>
            <li>
              <PhaseBanner />
            </li>
            {/* ToDo: 時計、電源状態 など */}
            <li className="red-255 white-255-text">
              <button onClick={onEStop}>
                <span className="symbol yellow-255-text">◆</span> EMG STOP
              </button>
            </li>
          </ul>
        </div>

        {rejection ? (
          <div
            className="tui-window red-168"
            style={{
              position: "fixed",
              left: "50%",
              transform: "translateX(-50%)",
              bottom: "3rem",
              zIndex: 60,
              maxWidth: "calc(100vw - 2rem)",
            }}
          >
            <fieldset className="tui-fieldset">
              <legend>[!] 操作が拒否されました</legend>
              <div>{rejection.reason}</div>
              <div style={{ opacity: 0.8 }}>command: {rejection.command}</div>
            </fieldset>
          </div>
        ) : null}

        <EStopOverlay />
      </div>
    </RobotProvider>
  );
}
