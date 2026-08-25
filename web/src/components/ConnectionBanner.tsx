import { Button } from "@tsaito18/tuicss-react";

import { useRobot } from "@/context/RobotContext";

/**
 * WebSocket 切断の全画面幅バナー。
 *
 * 切断中は全操作がサーバーに届かず、画面上の値も更新が止まったまま残る。
 * ステータスバーの小さな文字では気付けないため、画面上端を占有して知らせる。
 * useRobotSocket が 3 秒間隔で自動再接続するので、操縦者側の操作は不要。
 */
export function ConnectionBanner() {
  const { connected, wsUrl, openWsSettings } = useRobot();

  if (connected) return null;

  return (
    <div role="alert" className="connection-banner">
      {/* 点滅は先頭の記号だけに留める。文章まで点滅すると読み取りに時間がかかる */}
      <span className="alert-blink">◆</span> 通信切断 —
      サーバーに接続できません。表示中の値は最新ではありません (自動再接続中...){" "}
      {/* 繋ぎ先違いが原因のことがあるため、切断表示から直接確認・変更できるようにする */}
      <Button type="button" onClick={openWsSettings}>
        接続先 {wsUrl} を変更
      </Button>
    </div>
  );
}
