import { memo, useCallback, useMemo, useState } from "react";
import { Outlet, useLocation, useNavigate } from "react-router";

import { AppHeader } from "@/components/shell/AppHeader";
import { ConnectionBanner } from "@/components/shell/ConnectionBanner";
import { EStopBanner } from "@/components/shell/EStopBanner";
import { EStopOverlay } from "@/components/shell/EStopOverlay";
import { RouteErrorBoundary } from "@/components/shell/RouteErrorBoundary";
import { Toaster } from "@/components/shell/Toaster";
import { WsSettings } from "@/components/shell/WsSettings";
import { ModalProvider } from "@/context/ModalContext";
import { RobotProvider } from "@/context/RobotContext";
import { useHotkeys } from "@/hooks/useHotkeys";
import { useRobotSocket } from "@/hooks/useRobotSocket";
import { useWsUrl } from "@/hooks/useWsUrl";
import type { MatchCourt } from "@/lib/protocol";
import { TABS } from "@/lib/tabs";

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

function RoutedOutlet() {
  const { pathname } = useLocation();
  return (
    <RouteErrorBoundary key={pathname}>
      <Outlet />
    </RouteErrorBoundary>
  );
}

const AppShell = memo(function AppShell({
  wsSettingsOpen,
  onCloseWsSettings,
}: {
  wsSettingsOpen: boolean;
  onCloseWsSettings: () => void;
}) {
  return (
    <ModalProvider>
      <TabHotkeys />
      <div className="flex h-svh w-full flex-col overflow-hidden bg-base-200 text-base-content">
        <ConnectionBanner />
        <EStopBanner />
        <AppHeader />

        <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
          <RoutedOutlet />
        </div>

        <Toaster />
        <WsSettings open={wsSettingsOpen} onClose={onCloseWsSettings} />
        <EStopOverlay />
      </div>
    </ModalProvider>
  );
});

export function RootLayout() {
  const { wsUrl, wsUrlSource, setWsUrl, resetWsUrl } = useWsUrl();
  const socket = useRobotSocket(wsUrl);
  const { send, clearRejection, setEStopActive, reportUnsent } = socket;
  const [wsSettingsOpen, setWsSettingsOpen] = useState(false);
  const [overlayHidden, setOverlayHidden] = useState(false);
  const hideEStopOverlay = useCallback(() => setOverlayHidden(true), []);
  // 保持した値をそのまま使わず毎描画で dev_tools と AND を取る。フラグ配信が壊れた瞬間や
  // 本番サーバーへ繋ぎ直した瞬間に、操縦者の操作を待たずオーバーレイが戻る。
  const eStopOverlayHidden = socket.serverInfo.dev_tools && overlayHidden;
  const openWsSettings = useCallback(() => setWsSettingsOpen(true), []);
  const closeWsSettings = useCallback(() => setWsSettingsOpen(false), []);

  const onEStop = useCallback(() => {
    if (send({ type: "e_stop" })) {
      setEStopActive(true);
      return;
    }
    reportUnsent(
      "e_stop",
      "切断中のため緊急停止を送信できませんでした。機体は停止していません — 機体側の非常停止で止めてください",
    );
  }, [send, setEStopActive, reportUnsent]);

  const onEStopRelease = useCallback(() => {
    if (send({ type: "e_stop_release" })) {
      setEStopActive(false);
      return;
    }
    reportUnsent(
      "e_stop_release",
      "切断中のため緊急停止の解除を送信できませんでした。機体側のラッチは残っています",
    );
  }, [send, setEStopActive, reportUnsent]);

  const sendOrReport = useCallback(
    (data: Record<string, unknown> & { type: string }, what: string) => {
      if (send(data)) return true;
      reportUnsent(data.type, `切断中のため${what}を送信できませんでした`);
      return false;
    },
    [send, reportUnsent],
  );

  const setCourt = useCallback(
    (court: MatchCourt) => {
      sendOrReport({ type: "set_court", court }, "コート設定");
    },
    [sendOrReport],
  );
  const matchStart = useCallback(() => {
    sendOrReport({ type: "match_start" }, "試合開始");
  }, [sendOrReport]);
  const matchFinish = useCallback(() => {
    sendOrReport({ type: "match_finish" }, "試合終了");
  }, [sendOrReport]);
  const matchReset = useCallback(() => {
    sendOrReport({ type: "match_reset" }, "リセット");
  }, [sendOrReport]);

  return (
    <RobotProvider
      value={{
        states: socket.states,
        connected: socket.connected,
        eStopActive: socket.eStopActive,
        eStopReason: socket.eStopReason,
        eStopOverlayHidden,
        healthEvents: socket.healthEvents,
        motorCheck: socket.motorCheck,
        homing: socket.homing,
        returnHome: socket.returnHome,
        switchMeasure: socket.switchMeasure,
        matchState: socket.matchState,
        serverInfo: socket.serverInfo,
        rejection: socket.rejection,
        link: socket.link,
        clearRejection,
        wsUrl,
        wsUrlSource,
        setWsUrl,
        resetWsUrl,
        openWsSettings,
        send,
        sendOrReport,
        onEStop,
        onEStopRelease,
        hideEStopOverlay,
        setCourt,
        matchStart,
        matchFinish,
        matchReset,
      }}
    >
      <AppShell wsSettingsOpen={wsSettingsOpen} onCloseWsSettings={closeWsSettings} />
    </RobotProvider>
  );
}
