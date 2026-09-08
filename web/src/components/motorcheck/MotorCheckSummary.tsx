import { StatusBadge } from "@/components/ui/StatusBadge";
import { useRobotStatus } from "@/context/RobotContext";
import { useMotorCheck } from "@/hooks/useMotorCheck";
import { motorCheckStatus } from "@/lib/motorCheckStatus";
import { MALFORMED } from "@/lib/protocol";
import type { MotorCheckSnapshot } from "@/lib/protocol";

/**
 * 除外の有無を 1 語で添える。**「完了」だけを出してはならない** ——
 * 除外されたステップは走らないので、全ステップ成功と「サブハンドを丸ごと
 * 飛ばした成功」が同じ表示になる。内訳はパネルが出す。
 *
 * 実行中・失敗時に添えないのは、そのあいだ `MotorCheckPanel` が強制的に開いて
 * いて内訳がそのまま見えているため (同じ事実を数行のあいだに 2 度並べない)。
 * 逆に完了・未実行ではパネルが畳まれるので、除外があることを言えるのはここだけになる。
 */
function ExcludedNote({ state }: { state: MotorCheckSnapshot }) {
  if (state.excluded_steps === MALFORMED) {
    return <span className="text-warning">除外 判定不能</span>;
  }
  if (state.excluded_steps.length === 0) return null;
  return <span className="text-warning">{state.excluded_steps.length} ステップ除外</span>;
}

/**
 * ステップ一覧が読めなかったことを 1 語で添える。**畳んだ状態でも見えている必要がある。**
 *
 * 内訳 (「ステップ一覧を読み取れませんでした」の全文) は `MotorCheckPanel` が出すが、
 * あちらが自分から開くのは実行中と失敗時だけで、**未実行・完了では畳まれたまま**になる。
 * ところが完了判定 (`motorCheckStatus`) が見るのは `total_steps` だけなので、
 * `steps` が読めないまま `step_index >= total_steps` の配信が届くと、ここは緑の「完了」を
 * 出し、パネルは畳まれたままになる —— 操縦者は警告を一度も見ずに指差喚呼
 * 「アクチュエータ動作確認 完了」にチェックを付けられる。`ExcludedNote` と同じ理由で、
 * ここが言えなければ誰も言わない。
 */
function StepsNote({ state }: { state: MotorCheckSnapshot }) {
  if (state.steps !== MALFORMED) return null;
  return <span className="text-warning">ステップ 判定不能</span>;
}

/**
 * 統合動作確認の状態を区分見出しに 1 語で出す。
 *
 * 指差喚呼には「アクチュエータ動作確認 完了」の項目がある。チェックを付ける前に
 * 「そもそも完走したのか」だけは、項目の隣で読み切れる必要がある。
 *
 * **状態しか出さない。** 進み具合 (何 / 何) と失敗理由は同じ区分の中で
 * `MotorCheckPanel` が出す —— パネルは同じ区分の中にあるので、ここでも出すと
 * 同じ事実が数行のあいだに 2 度並ぶ (しかも片方は truncate された半端な理由になる)。
 *
 * 判定は `lib/motorCheckStatus.ts` が持つ。こことパネルで別々に判定すると、
 * 同じ瞬間にパネルが「完了」、ここが「未実行」を出す。
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
