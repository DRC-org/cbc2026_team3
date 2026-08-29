import { memo, useCallback, useMemo, useState } from "react";
import { Outlet, useLocation, useNavigate } from "react-router";

import { AppHeader } from "@/components/shell/AppHeader";
import { ConnectionBanner } from "@/components/shell/ConnectionBanner";
import { EStopOverlay } from "@/components/shell/EStopOverlay";
import { StatusBar } from "@/components/shell/StatusBar";
import { Toaster } from "@/components/shell/Toaster";
import { WsSettings } from "@/components/shell/WsSettings";
import { ModalProvider } from "@/context/ModalContext";
import { RobotProvider } from "@/context/RobotContext";
import { useHotkeys } from "@/hooks/useHotkeys";
import { useRobotSocket } from "@/hooks/useRobotSocket";
import { useWsUrl } from "@/hooks/useWsUrl";
import type { ChecklistRole, MatchCourt } from "@/lib/protocol";
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
 * 外枠の中身。**memo が本体で、飾りではない。**
 *
 * RootLayout は 20Hz × 2 台のテレメトリで毎秒 40 回再描画される。子要素は
 * 再描画のたびに作り直されるため、memo が無いと context をいくつに割っても
 * 部分木全体が同じ頻度で描き直される (React は memo 無しの子を素通しで再描画する)。
 * ここで止めておくと、以降で実際に動くのは購読している部品だけになる。
 *
 * したがって props は「滅多に変わらない値」だけに保つこと。
 * テレメトリ由来の値をここへ渡した瞬間に memo は無効になる。
 */
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
        <WsSettings open={wsSettingsOpen} onClose={onCloseWsSettings} />
        <EStopOverlay />
      </div>
    </ModalProvider>
  );
});

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
  const { send, clearRejection, setEStopActive, reportUnsent } = socket;
  const [wsSettingsOpen, setWsSettingsOpen] = useState(false);
  const openWsSettings = useCallback(() => setWsSettingsOpen(true), []);
  const closeWsSettings = useCallback(() => setWsSettingsOpen(false), []);

  // 依存に socket (毎描画 新しいオブジェクト) を置くと useCallback が実質無効になり、
  // コマンド購読が毎秒 40 回変わる。個々の関数だけを依存にすること
  //
  // 楽観的更新は「送れたとき」だけ行う。切断中に無条件で状態を変えると、
  // 何も送っていないのに全画面へ赤い停止オーバーレイが出る。オーバーレイは
  // inset:0 なので、矛盾を示すはずの接続バナーごと覆い隠してしまう。
  // かといって黙って捨てるのも危険なので、送れなかったことは通知枠へ流す
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
    // ここでオーバーレイだけ閉じると、機体側のラッチが残ったまま画面は平常へ戻る
    reportUnsent(
      "e_stop_release",
      "切断中のため緊急停止の解除を送信できませんでした。機体側のラッチは残っています",
    );
  }, [send, setEStopActive, reportUnsent]);

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
  // 開発用。サーバー側が --dev-tools 起動でなければ command_rejected で返ってくる
  const checkAllChecklist = useCallback(
    (role: ChecklistRole) => send({ type: "checklist_check_all", role }),
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
        eStopReason: socket.eStopReason,
        healthEvents: socket.healthEvents,
        motorCheck: socket.motorCheck,
        matchState: socket.matchState,
        serverInfo: socket.serverInfo,
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
        checkAllChecklist,
        matchStart,
        matchFinish,
        matchReset,
      }}
    >
      <AppShell wsSettingsOpen={wsSettingsOpen} onCloseWsSettings={closeWsSettings} />
    </RobotProvider>
  );
}
