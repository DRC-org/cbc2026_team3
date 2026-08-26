import { useCallback, useMemo, useState } from "react";
import { Outlet, useLocation, useNavigate } from "react-router";

import { AppHeader } from "@/components/AppHeader";
import { ConnectionBanner } from "@/components/ConnectionBanner";
import { EStopOverlay } from "@/components/EStopOverlay";
import { StatusBar } from "@/components/StatusBar";
import { Toaster } from "@/components/Toaster";
import { WsSettings } from "@/components/WsSettings";
import { ModalProvider } from "@/context/ModalContext";
import { RobotProvider } from "@/context/RobotContext";
import { useHotkeys } from "@/hooks/useHotkeys";
import { useRobotSocket } from "@/hooks/useRobotSocket";
import type { ChecklistRole, MatchCourt } from "@/hooks/useRobotSocket";
import { useWsUrl } from "@/hooks/useWsUrl";
import { TABS } from "@/lib/tabs";

/**
 * 数字キーによるタブ移動。
 * ModalProvider の内側に置くことで、モーダル表示中は発火しない
 * （緊急停止オーバーレイの裏でタブが動くのを防ぐ）。
 */
function TabHotkeys() {
  const navigate = useNavigate();
  const { search } = useLocation();

  const tabHotkeys = useMemo(
    () =>
      Object.fromEntries(
        TABS.map((tab) => [tab.hotkey, () => navigate({ pathname: tab.path, search })] as const),
      ),
    [navigate, search],
  );
  useHotkeys(tabHotkeys);

  return null;
}

/**
 * 全画面共通の外枠。
 *
 * WebSocket 接続と RobotProvider をここに置くことで、タブ (子ルート) を切り替えても
 * 再接続が起きない。子ルートは常に 1 つだけ描画されるため、RobotControl の
 * Space ホットキーが表示中のロボットにだけ届く前提もそのまま維持される。
 */
export function RootLayout() {
  const { wsUrl, wsUrlSource, setWsUrl, resetWsUrl } = useWsUrl();
  const socket = useRobotSocket(wsUrl);
  const { send, clearRejection } = socket;
  const [wsSettingsOpen, setWsSettingsOpen] = useState(false);
  const openWsSettings = useCallback(() => setWsSettingsOpen(true), []);
  const closeWsSettings = useCallback(() => setWsSettingsOpen(false), []);

  const onEStop = useCallback(() => {
    send({ type: "e_stop" });
    socket.setEStopActive(true);
  }, [send, socket]);

  const onEStopRelease = useCallback(() => {
    send({ type: "e_stop_release" });
    socket.setEStopActive(false);
  }, [send, socket]);

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
        wsUrl,
        wsUrlSource,
        setWsUrl,
        resetWsUrl,
        openWsSettings,
        send,
        onEStop,
        onEStopRelease,
        setCourt,
        setChecklistItem,
        resetChecklist,
        matchStart,
        matchFinish,
        matchReset,
      }}
    >
      <ModalProvider>
        <TabHotkeys />
        {/* 20px 固定だと 1366x768 級のノート PC でパネルが画面外に溢れる。
            ページ全体はスクロールさせず、常に 1 画面へ収める */}
        <div className="flex h-svh w-full flex-col overflow-hidden bg-base-200 text-base-content">
          <ConnectionBanner />
          {/* タブは AppHeader の中。帯を 2 段消費しないよう 1 段に畳んである */}
          <AppHeader />

          <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
            <Outlet />
          </div>

          <StatusBar />

          <Toaster />
          <WsSettings open={wsSettingsOpen} onClose={closeWsSettings} />
          <EStopOverlay />
        </div>
      </ModalProvider>
    </RobotProvider>
  );
}
