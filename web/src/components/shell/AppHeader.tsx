import { OctagonX } from "lucide-react";

import { Clock } from "@/components/shell/Clock";
import { TabBar } from "@/components/shell/TabBar";
import { Icon } from "@/components/ui/Icon";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { useRobotCommands, useRobotStatus } from "@/context/RobotContext";
import { cx } from "@/lib/cx";
import { COURT_LABEL, COURT_TONE, PHASE_BAND_CLASS, PHASE_LABEL, PHASE_TONE } from "@/lib/phase";
import { TONE_STATUS_CLASS } from "@/lib/tone";

function wsHostLabel(url: string): string {
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
}

export function AppHeader() {
  const { connected, matchState, wsUrl } = useRobotStatus();
  const { onEStop, openWsSettings } = useRobotCommands();
  const { court, phase } = matchState;

  return (
    <header
      className={cx(
        "flex shrink-0 items-stretch border-b border-l-[0.4rem] border-base-300 bg-base-100",
        PHASE_BAND_CLASS[phase],
      )}
    >
      <div className="flex min-w-0 flex-1 items-center gap-x-3 px-2 py-1">
        <div className="min-w-0 overflow-hidden">
          <TabBar />
        </div>

        <div className="ml-auto flex shrink-0 items-center gap-3">
          <div className="flex shrink-0 items-center gap-3 text-[0.82em] text-base-content/70">
            <button
              type="button"
              onClick={openWsSettings}
              className="flex cursor-pointer items-center gap-1.5 hover:text-base-content"
              title={`接続先: ${wsUrl}（クリックで変更）`}
            >
              <span
                className={cx(TONE_STATUS_CLASS[connected ? "success" : "error"], "status-sm")}
              />
              {connected ? "Connected" : "Disconnected"}
              <span className="font-mono">{wsHostLabel(wsUrl)}</span>
            </button>

            <Clock />
          </div>

          <div className="flex shrink-0 items-center gap-1.5">
            <StatusBadge tone={PHASE_TONE[phase]}>{PHASE_LABEL[phase]}</StatusBadge>
            <StatusBadge tone={COURT_TONE[court]}>{COURT_LABEL[court]}</StatusBadge>
          </div>
        </div>
      </div>

      <button
        type="button"
        className="ml-6 flex shrink-0 cursor-pointer items-center gap-2 bg-estop px-6 text-[1.1em] font-bold text-estop-fg hover:bg-[#a82418] focus-visible:outline-2 focus-visible:outline-offset-[-4px] focus-visible:outline-estop-fg"
        onClick={onEStop}
        aria-label="緊急停止"
      >
        <Icon as={OctagonX} className="text-[1.25em]" />
        EMG STOP
      </button>
    </header>
  );
}
