import { TriangleAlert } from "lucide-react";

import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
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
    <div
      role="alert"
      className="alert flex w-full shrink-0 items-center justify-center gap-3 border-x-0 border-t-0 px-3 py-1 alert-error"
    >
      {/* 点滅は先頭の記号だけに留める。文章まで点滅すると読み取りに時間がかかる */}
      <Icon as={TriangleAlert} className="alert-blink text-[1.2em]" />
      <span className="font-bold">
        通信切断 — サーバーに接続できません。表示中の値は最新ではありません (自動再接続中...)
      </span>
      {/* 繋ぎ先違いが原因のことがあるため、切断表示から直接確認・変更できるようにする */}
      <Button tone="estopReset" onClick={openWsSettings}>
        接続先 {wsUrl} を変更
      </Button>
    </div>
  );
}
