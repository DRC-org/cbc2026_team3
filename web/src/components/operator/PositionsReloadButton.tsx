import { RefreshCw } from "lucide-react";

import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { StatusBadge } from "@/components/ui/StatusBadge";
import type { RobotCommands } from "@/context/RobotContext";
import type { Malformed, PositionsReloadState } from "@/lib/protocol";
import { MALFORMED } from "@/lib/protocol";
import { formatClock } from "@/lib/time";

interface PositionsReloadButtonProps {
  robotKey: string;
  reload: PositionsReloadState | Malformed | null;
  sendOrReport: RobotCommands["sendOrReport"];
}

/** 位置定数 yaml を走らせたまま読み直す口。控える面は消したが、これだけは残す。 */
export function PositionsReloadButton({
  robotKey,
  reload,
  sendOrReport,
}: PositionsReloadButtonProps) {
  if (reload === null) return null;
  if (reload === MALFORMED) {
    return <StatusBadge tone="error">読み直しの状態 判定不能</StatusBadge>;
  }
  return (
    <div className="flex shrink-0 flex-wrap items-center gap-2">
      <Button
        aria-label="位置定数 yaml を読み直す"
        title="yaml の値をサーバーへ読ませる（再起動も CAN の入れ直しも要らない）"
        onClick={() =>
          sendOrReport({ type: "positions_reload", robot: robotKey }, "位置定数の読み直し")
        }
      >
        <Icon as={RefreshCw} />
        yaml を読み直す
      </Button>
      <span className="text-[0.85em] text-base-content/60" title={reload.changed.join(", ")}>
        {reload.reloaded_at === null
          ? ""
          : `${formatClock(reload.reloaded_at * 1000)} に読み直し・${
              reload.changed.length === 0 ? "変化なし" : `${reload.changed.length} 件変化`
            }`}
      </span>
    </div>
  );
}
