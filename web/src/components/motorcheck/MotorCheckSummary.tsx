import { StatusBadge } from "@/components/ui/StatusBadge";
import { useMotorCheck } from "@/hooks/useMotorCheck";

/**
 * 統合動作確認の状態を 1 行で出す。
 *
 * 指差喚呼には「アクチュエータ動作確認 完了」の項目がある。結果を知るために
 * 毎回モーダルを開かせるのは、チェックを付ける前の確認としては手数が多い。
 * 進み具合だけならここで読み切れる。
 */
export function MotorCheckSummary() {
  const { state } = useMotorCheck();

  if (state.running) {
    return (
      <span className="flex min-w-0 items-center gap-2">
        <StatusBadge tone="info">実行中</StatusBadge>
        <span className="font-mono text-base-content/70 tabular-nums">
          {state.step_index} / {state.total_steps}
        </span>
      </span>
    );
  }

  if (state.error) {
    return (
      <span className="flex min-w-0 items-center gap-2">
        <StatusBadge tone="warning">未完了</StatusBadge>
        <span className="min-w-0 truncate text-base-content/70">{state.error}</span>
      </span>
    );
  }

  // 完走したかは「最後まで進んだか」で見る。ステップ数 0 は未読込
  if (state.total_steps > 0 && state.step_index >= state.total_steps) {
    return <StatusBadge tone="success">完了</StatusBadge>;
  }

  return <span className="text-base-content/60">未実行</span>;
}
