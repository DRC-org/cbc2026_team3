import { Check, X } from "lucide-react";

import { Icon } from "@/components/ui/Icon";
import { MalformedNotice } from "@/components/ui/MalformedNotice";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { useRobotStatus } from "@/context/RobotContext";
import { useHoming } from "@/hooks/useHoming";
import { homingEntry, homingStatus } from "@/lib/homingStatus";
import type { HomingEntry } from "@/lib/homingStatus";
import { MALFORMED } from "@/lib/protocol";
import { robotLabel } from "@/lib/robotLabel";

interface HomingPanelProps {
  /** 操縦者画面では他機の結果を出さない (Monitor では省略して全機) */
  robot?: string;
}

export function HomingPanel({ robot: only }: HomingPanelProps = {}) {
  const { connected } = useRobotStatus();
  const { state } = useHoming();

  if (state.robots === MALFORMED) {
    return <MalformedNotice subject="零点合わせの状態" />;
  }

  const robots = only === undefined ? Object.keys(state.robots) : [only];

  return (
    <>
      {robots.map((robot) => (
        <HomingRobotPanel
          key={robot}
          robot={robot}
          entry={homingEntry(state, robot)}
          connected={connected}
        />
      ))}
    </>
  );
}

interface HomingRobotPanelProps {
  robot: string;
  entry: HomingEntry;
  connected: boolean;
}

function HomingRobotPanel({ robot, entry, connected }: HomingRobotPanelProps) {
  const { outcome, failures } = homingStatus(entry, connected);

  if (entry === undefined || entry === MALFORMED || outcome === "idle") return null;

  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-center gap-2">
        <span className="text-base-content/70">零点合わせ</span>
        <span className="min-w-0 truncate font-medium">{robotLabel(robot)}</span>
        {outcome === "running" ? (
          <StatusBadge tone="info">実行中</StatusBadge>
        ) : outcome === "failed" ? (
          <StatusBadge tone="warning">未完了</StatusBadge>
        ) : outcome === "done" ? (
          <StatusBadge tone="success">完了</StatusBadge>
        ) : null}
        {entry.running && entry.current_axis ? (
          <span className="min-w-0 truncate font-mono text-info">{entry.current_axis}</span>
        ) : null}
      </div>

      {entry.error ? <p className="text-error">{entry.error}</p> : null}

      {entry.results === MALFORMED || failures === MALFORMED ? (
        <MalformedNotice subject="零点合わせの結果" />
      ) : (
        <ul className="flex flex-col">
          {entry.results.map((result) => (
            <li key={result.axis} className="flex items-baseline gap-2 px-1 py-[0.15rem]">
              <Icon
                as={result.error === null ? Check : X}
                className={result.error === null ? "text-success" : "text-error"}
              />
              <span className="font-mono">{result.axis}</span>
              {result.error ? <span className="min-w-0 text-error">{result.error}</span> : null}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
