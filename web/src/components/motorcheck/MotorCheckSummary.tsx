import { StatusBadge } from "@/components/ui/StatusBadge";
import { useRobotStatus } from "@/context/RobotContext";
import { useMotorCheck } from "@/hooks/useMotorCheck";
import { motorCheckStatus } from "@/lib/motorCheckStatus";
import { MALFORMED } from "@/lib/protocol";
import type { MotorCheckSnapshot } from "@/lib/protocol";

/**
 * 除外の有無を 1 語で添える。**「完了」だけを出してはならない** ——
 * 除外されたステップは走らないので、全ステップ成功と「サブハンドを丸ごと
 * 飛ばした成功」が同じ表示になる。件数と内訳はパネルが出す。
 */
function ExcludedNote({ state }: { state: MotorCheckSnapshot }) {
  if (state.excluded_steps === MALFORMED) {
    return <span className="text-warning">除外 判定不能</span>;
  }
  if (state.excluded_steps.length === 0) return null;
  return <span className="text-warning">{state.excluded_steps.length} ステップ除外</span>;
}

/**
 * 統合動作確認の状態を 1 行で出す。
 *
 * 指差喚呼には「アクチュエータ動作確認 完了」の項目がある。結果を知るために
 * 毎回モーダルを開かせるのは、チェックを付ける前の確認としては手数が多い。
 * 進み具合だけならここで読み切れる。
 *
 * 判定は `lib/motorCheckStatus.ts` が持つ。こことパネルで別々に判定していた頃は、
 * 同じ瞬間にパネルが「完了」、ここが「未実行」を出していた。
 */
export function MotorCheckSummary() {
  const { connected } = useRobotStatus();
  const { state } = useMotorCheck();
  const { outcome, failureReason } = motorCheckStatus(state, connected);

  if (outcome === "running") {
    return (
      <span className="flex min-w-0 items-center gap-2">
        <StatusBadge tone="info">実行中</StatusBadge>
        <span className="font-mono text-base-content/70 tabular-nums">
          {state.step_index} / {state.total_steps}
        </span>
      </span>
    );
  }

  if (outcome === "failed") {
    return (
      <span className="flex min-w-0 items-center gap-2">
        <StatusBadge tone="warning">未完了</StatusBadge>
        <span className="min-w-0 truncate text-base-content/70">{failureReason}</span>
        <ExcludedNote state={state} />
      </span>
    );
  }

  return (
    <span className="flex min-w-0 items-center gap-2">
      {outcome === "done" ? (
        <StatusBadge tone="success">完了</StatusBadge>
      ) : (
        <span className="text-base-content/60">未実行</span>
      )}
      <ExcludedNote state={state} />
    </span>
  );
}
