import { StatusBadge } from "@/components/ui/StatusBadge";
import { useRobotStatus } from "@/context/RobotContext";
import { useMotorCheck } from "@/hooks/useMotorCheck";
import { motorCheckStatus } from "@/lib/motorCheckStatus";

/**
 * 統合動作確認の状態を区分見出しに 1 語で出す。
 *
 * 指差喚呼には「アクチュエータ動作確認 完了」の項目がある。チェックを付ける前に
 * 「そもそも完走したのか」だけは、項目の隣で読み切れる必要がある。
 *
 * **状態しか出さない。** 進み具合 (何 / 何) と失敗理由は同じ区分の中で
 * `MotorCheckPanel` が出す —— かつてパネルがモーダルだった頃はここが唯一の
 * 常時表示だったので進捗も理由も持っていたが、インライン化した今は同じ事実が
 * 数行のあいだに 2 度並ぶ (しかも片方は truncate された半端な理由になる)。
 *
 * 判定は `lib/motorCheckStatus.ts` が持つ。こことパネルで別々に判定していた頃は、
 * 同じ瞬間にパネルが「完了」、ここが「未実行」を出していた。
 */
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

  if (outcome === "done") {
    return <StatusBadge tone="success">完了</StatusBadge>;
  }

  return <span className="text-base-content/60">未実行</span>;
}
