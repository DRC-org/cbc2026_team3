import { Tab, TabList, TabPanel, Tabs } from "@tsaito18/tuicss-react";
import { useCallback } from "react";

import { EStopOverlay } from "@/components/EStopOverlay";
import { RobotProvider } from "@/context/RobotContext";
import { useRobotSocket } from "@/hooks/useRobotSocket";
import { Dashboard } from "@/pages/Dashboard";
import { MotorTuning } from "@/pages/MotorTuning";
import { RobotControl } from "@/pages/RobotControl";

export function App() {
  const socket = useRobotSocket();

  const onEStop = useCallback(() => {
    socket.send({ type: "e_stop" });
    socket.setEStopActive(true);
  }, [socket]);

  const onEStopRelease = useCallback(() => {
    socket.send({ type: "e_stop_release" });
    socket.setEStopActive(false);
  }, [socket]);

  return (
    <RobotProvider
      value={{
        states: socket.states,
        connected: socket.connected,
        eStopActive: socket.eStopActive,
        healthEvents: socket.healthEvents,
        motorChecks: socket.motorChecks,
        send: socket.send,
        onEStop,
        onEStopRelease,
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
            {/* ToDo: 時計、電源状態 など */}
            <li className="red-255 white-255-text">
              <button onClick={onEStop}>
                <span className="symbol yellow-255-text">◆</span> EMG STOP
              </button>
            </li>
          </ul>
        </div>
        <EStopOverlay />
      </div>
    </RobotProvider>
  );
}
