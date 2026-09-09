import { Check, TriangleAlert, X } from "lucide-react";

import { Icon } from "@/components/ui/Icon";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { useRobotStatus } from "@/context/RobotContext";
import { useHoming } from "@/hooks/useHoming";
import { homingStatus } from "@/lib/homingStatus";
import { MALFORMED } from "@/lib/protocol";
import { robotLabel } from "@/lib/robotLabel";

interface HomingPanelProps {
  /** 操縦者画面では他機の結果を出さない (Monitor では省略して全機) */
  robot?: string;
}

export function HomingPanel({ robot: only }: HomingPanelProps = {}) {
  const { connected } = useRobotStatus();
  const { state } = useHoming();
  const { outcome, failures } = homingStatus(state, connected);

  if (outcome === "idle" && state.robot === null) return null;
  if (only !== undefined && state.robot !== only) return null;

  const robot = state.robot === null ? "" : robotLabel(state.robot);

  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-center gap-2">
        <span className="text-base-content/70">零点合わせ</span>
        <span className="min-w-0 truncate font-medium">{robot}</span>
        {outcome === "running" ? (
          <StatusBadge tone="info">実行中</StatusBadge>
        ) : outcome === "failed" ? (
          <StatusBadge tone="warning">未完了</StatusBadge>
        ) : outcome === "done" ? (
          <StatusBadge tone="success">完了</StatusBadge>
        ) : null}
        {state.running && state.current_axis ? (
          <span className="min-w-0 truncate font-mono text-info">{state.current_axis}</span>
        ) : null}
      </div>

      {state.error ? <p className="text-error">{state.error}</p> : null}

      {state.results === MALFORMED || failures === MALFORMED ? (
        <p className="flex items-center gap-1.5 text-warning">
          <Icon as={TriangleAlert} />
          零点合わせの結果を読み取れませんでした (配信の形が読めていません)
        </p>
      ) : (
        <ul className="flex flex-col">
          {state.results.map((result) => (
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
