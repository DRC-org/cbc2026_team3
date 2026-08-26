import { StatusBadge } from "@/components/ui/StatusBadge";
import { useMotorCheck } from "@/hooks/useMotorCheck";
import type { MotorCheckOverall } from "@/lib/protocol";
import { formatClock } from "@/lib/time";
import type { Tone } from "@/lib/tone";

const OVERALL: Record<MotorCheckOverall, { tone: Tone; label: string }> = {
  running: { tone: "info", label: "実行中" },
  ok: { tone: "success", label: "全モータ合格" },
  partial: { tone: "warning", label: "一部失敗" },
  failed: { tone: "error", label: "失敗" },
};

/**
 * 直近の動作確認の結果を 1 行で出す。
 *
 * 指差喚呼には「アクチュエータ動作確認 完了」の項目がある。結果を知るために
 * 毎回モーダルを開かせるのは、チェックを付ける前の確認としては手数が多い。
 * 合否と時刻だけならここで読み切れる。
 */
export function MotorCheckSummary({ robotName }: { robotName: string }) {
  const { state } = useMotorCheck(robotName);

  if (state.status === "idle" && !state.snapshot) {
    return <span className="text-base-content/60">未実行</span>;
  }

  if (state.status === "error") {
    return (
      <span className="flex min-w-0 items-center gap-2">
        <StatusBadge tone="error">エラー</StatusBadge>
        <span className="min-w-0 truncate text-base-content/70">{state.error}</span>
      </span>
    );
  }

  const overall = state.snapshot?.overall ?? (state.status === "running" ? "running" : null);
  if (!overall) return <span className="text-base-content/60">未実行</span>;

  const style = OVERALL[overall];
  const passed = state.records.filter((r) => r.result === "passed").length;

  return (
    <span className="flex min-w-0 flex-wrap items-center gap-2">
      <StatusBadge tone={style.tone}>{style.label}</StatusBadge>
      <span className="font-mono text-base-content/70 tabular-nums">
        {passed}/{state.records.length}
      </span>
      <span className="text-base-content/60">
        {formatClock(state.finishedAtMs ?? state.startedAtMs)}
      </span>
    </span>
  );
}
