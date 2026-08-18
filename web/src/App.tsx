import { Tab, TabList, TabPanel, Tabs } from "@tsaito18/tuicss-react";
import { useCallback, useEffect, useMemo, useState } from "react";

import { AppHeader } from "@/components/AppHeader";
import { ConnectionBanner } from "@/components/ConnectionBanner";
import { EStopOverlay } from "@/components/EStopOverlay";
import { Toaster } from "@/components/Toaster";
import { RobotProvider } from "@/context/RobotContext";
import { useHotkeys } from "@/hooks/useHotkeys";
import { useRobotSocket } from "@/hooks/useRobotSocket";
import type { ChecklistRole, MatchCourt, MatchMode, RobotState } from "@/hooks/useRobotSocket";
import { Dashboard } from "@/pages/Dashboard";
import { MotorTuning } from "@/pages/MotorTuning";
import { RobotControl } from "@/pages/RobotControl";

interface TabDef {
  value: string;
  label: string;
  /** 切替に割り当てる数字キー。TabList 上にも表示して発見できるようにする */
  hotkey: string;
  /** バッジ表示のために監視するロボット。Monitor / PID Tuning は対象外 */
  robotKey?: string;
}

const TABS: TabDef[] = [
  { value: "monitor", label: "Monitor", hotkey: "1" },
  { value: "main-hand", label: "Main Hand", hotkey: "2", robotKey: "main_hand" },
  { value: "sub-hand", label: "Sub Hand", hotkey: "3", robotKey: "sub_hand" },
  { value: "pid-tuning", label: "PID Tuning", hotkey: "4" },
];

const TAB_VALUES = new Set(TABS.map((tab) => tab.value));

/**
 * 表示中のタブを URL ハッシュに載せる。
 * 操縦者が担当タブを開いたままブラウザが落ちても、リロードで同じタブへ復帰できる。
 * 各操縦者が自分の URL をブックマークしておける利点もある。
 */
function readTabFromHash(): string {
  const hash = window.location.hash.replace(/^#/, "");
  return TAB_VALUES.has(hash) ? hash : TABS[0].value;
}

/**
 * タブに出す注意喚起バッジ。
 * 操縦者は自分のタブに張り付くため、他機がトリガー待ちや異常状態でも気付けない。
 * 切り替えなくても異変が分かるよう、タブラベル側に状態を出す。
 */
function tabBadge(state: RobotState | undefined): { symbol: string; className: string } | null {
  if (!state) return null;
  const health = state.health;
  if (health && health.overall !== "ok") {
    return { symbol: "⚠", className: "red-255-text" };
  }
  if (state.waiting_trigger) {
    return { symbol: "!", className: "yellow-255-text" };
  }
  return null;
}

function Clock() {
  const [now, setNow] = useState(() => new Date());

  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(id);
  }, []);

  return <span className="tabular-nums">{now.toLocaleTimeString("ja-JP", { hour12: false })}</span>;
}

export function App() {
  const socket = useRobotSocket();
  const { send, clearRejection } = socket;
  const [activeTab, setActiveTab] = useState(readTabFromHash);

  useEffect(() => {
    if (window.location.hash !== `#${activeTab}`) {
      window.history.replaceState(null, "", `#${activeTab}`);
    }
  }, [activeTab]);

  useEffect(() => {
    const onHashChange = () => setActiveTab(readTabFromHash());
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);

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

  const tabHotkeys = useMemo(
    () =>
      Object.fromEntries(TABS.map((tab) => [tab.hotkey, () => setActiveTab(tab.value)] as const)),
    [],
  );
  useHotkeys(tabHotkeys);

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
      {/* 地色を黒にして、パネルの外側（余白）が端末画面として自然に見えるようにする */}
      <div className="wrapper black-255 white-255-text">
        <ConnectionBanner />
        <AppHeader />

        <Tabs className="app-tabs" value={activeTab} onValueChange={setActiveTab}>
          <TabList>
            {TABS.map((tab) => {
              const badge = tab.robotKey ? tabBadge(socket.states[tab.robotKey]) : null;
              return (
                <Tab key={tab.value} value={tab.value}>
                  <span className="key-hint" style={{ marginLeft: 0, marginRight: "0.4em" }}>
                    {tab.hotkey}
                  </span>
                  {tab.label}
                  {badge ? <span className={badge.className}> [{badge.symbol}]</span> : null}
                </Tab>
              );
            })}
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

        <div className="tui-statusbar cyan-168">
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
              <Clock />
            </li>
            <li style={{ opacity: 0.75 }}>
              <span className="key-hint">1</span>
              <span className="key-hint">2</span>
              <span className="key-hint">3</span>
              <span className="key-hint">4</span> タブ切替
            </li>
            <li style={{ opacity: 0.75 }}>
              <span className="key-hint">Space</span> START / NEXT
            </li>
          </ul>
        </div>

        <Toaster />
        <EStopOverlay />
      </div>
    </RobotProvider>
  );
}
