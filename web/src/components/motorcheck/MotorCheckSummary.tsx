import { StatusBadge } from "@/components/ui/StatusBadge";
import { useRobotStatus } from "@/context/RobotContext";
import { useMotorCheck } from "@/hooks/useMotorCheck";
import { motorCheckStatus } from "@/lib/motorCheckStatus";
import { MALFORMED } from "@/lib/protocol";
import type { MotorCheckSnapshot } from "@/lib/protocol";

function ExcludedNote({ state }: { state: MotorCheckSnapshot }) {
  if (state.excluded_steps === MALFORMED) {
    return <span className="text-warning">除外 判定不能</span>;
  }
  if (state.excluded_steps.length === 0) return null;
  return <span className="text-warning">{state.excluded_steps.length} ステップ除外</span>;
}

function StepsNote({ state }: { state: MotorCheckSnapshot }) {
  if (state.steps !== MALFORMED) return null;
  return <span className="text-warning">ステップ 判定不能</span>;
}

export function MotorCheckSummary() {
  const { connected } = useRobotStatus();
  const { state } = useMotorCheck();
  const { outcome } = motorCheckStatus(state, connected);

  if (outcome === "running") {
    return <StatusBadge tone="info">実行中</StatusBadge>;
  }

  if (outcome === "failed") {
    return <StatusBadge tone="warning">未完了</StatusBadge>;
  }

  return (
    <span className="flex min-w-0 items-center gap-2">
      {outcome === "done" ? (
        <StatusBadge tone="success">完了</StatusBadge>
      ) : (
        <span className="text-base-content/60">未実行</span>
      )}
      <StepsNote state={state} />
      <ExcludedNote state={state} />
    </span>
  );
}
