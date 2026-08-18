import { useRobot } from "@/context/RobotContext";

/**
 * WebSocket 切断の全画面幅バナー。
 *
 * 切断中は全操作がサーバーに届かず、画面上の値も更新が止まったまま残る。
 * ステータスバーの小さな文字では気付けないため、画面上端を占有して知らせる。
 * useRobotSocket が 3 秒間隔で自動再接続するので、操縦者側の操作は不要。
 */
export function ConnectionBanner() {
  const { connected } = useRobot();

  if (connected) return null;

  return (
    <div
      role="alert"
      className="red-255 white-255-text"
      style={{
        flexShrink: 0,
        padding: "0.25rem 0.75rem",
        textAlign: "center",
        fontWeight: "bold",
      }}
    >
      {/* 点滅は先頭の記号だけに留める。文章まで点滅すると読み取りに時間がかかる */}
      <span className="alert-blink">◆</span> 通信切断 —
      サーバーに接続できません。表示中の値は最新ではありません (自動再接続中...)
    </div>
  );
}
