import { TriangleAlert } from "lucide-react";

import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { useRobotCommands, useRobotStatus } from "@/context/RobotContext";

export function ConnectionBanner() {
  const { connected, wsUrl } = useRobotStatus();
  const { openWsSettings } = useRobotCommands();

  if (connected) return null;

  return (
    <div
      role="alert"
      className="alert flex w-full shrink-0 items-center justify-center gap-3 border-x-0 border-t-0 px-3 py-1 alert-error"
    >
      <Icon as={TriangleAlert} className="alert-blink text-[1.2em]" />
      <span className="font-bold">
        通信切断 — サーバーに接続できません。表示中の値は最新ではありません (自動再接続中...)
      </span>
      <Button tone="estopReset" onClick={openWsSettings}>
        接続先 {wsUrl} を変更
      </Button>
    </div>
  );
}
