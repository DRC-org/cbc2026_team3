import { Check, RotateCcw } from "lucide-react";

import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { Panel } from "@/components/ui/Panel";
import { useRobot } from "@/context/RobotContext";
import type { ChecklistRole } from "@/hooks/useRobotSocket";
import { cx } from "@/lib/cx";
import { TONE_PROGRESS_CLASS } from "@/lib/tone";

interface ChecklistProps {
  checklistRole: ChecklistRole;
  title: string;
}

/**
 * セッティングタイム中の指差喚呼チェックリスト。
 * 状態はサーバー保持なので、同じロールを複数画面で開いても進捗が共有される。
 *
 * 上から順に指差して唱えながら潰していく運用なので、**次に唱える 1 項目**が
 * 分かることが最優先。全項目が同じ重さで並んでいると、どこまで進んだかを
 * 毎回目で数え直すことになる。未完の先頭だけを強調する。
 */
export function Checklist({ checklistRole, title }: ChecklistProps) {
  const { matchState, setChecklistItem, resetChecklist } = useRobot();
  const checklist = matchState.checklists[checklistRole];
  const locked = matchState.phase === "match" || matchState.phase === "finished";

  const items = checklist?.items ?? [];
  const checkedCount = items.filter((i) => i.checked).length;
  const completed = checklist?.completed ?? false;
  const percent = items.length > 0 ? (checkedCount / items.length) * 100 : 0;
  const nextIndex = items.findIndex((i) => !i.checked);

  return (
    <Panel
      legend={title}
      bodyClassName="p-0"
      actions={
        <Button
          disabled={locked || checkedCount === 0}
          onClick={() => resetChecklist(checklistRole)}
          aria-label={`${title} のチェックをすべて解除`}
        >
          <Icon as={RotateCcw} />
          CLEAR
        </Button>
      }
    >
      {/* 件数と進捗バーで「あと何項目か」を数えずに読ませる */}
      <div className="flex shrink-0 items-center gap-3 border-b border-base-300 px-2 py-1">
        <span className="font-mono text-[1.3em] tabular-nums">
          {checkedCount}
          <span className="text-base-content/45">/{items.length}</span>
        </span>
        <progress
          className={cx(
            "progress h-[0.5rem] flex-1 rounded-none bg-base-200",
            completed ? TONE_PROGRESS_CLASS.success : TONE_PROGRESS_CLASS.warning,
          )}
          value={percent}
          max={100}
        />
        {completed ? (
          <span className="flex shrink-0 items-center gap-1 font-medium text-success">
            <Icon as={Check} />
            完了
          </span>
        ) : (
          <span className="shrink-0 text-base-content/70">残り {items.length - checkedCount}</span>
        )}
      </div>

      {items.length === 0 ? (
        <p className="p-2 text-base-content/70">チェック項目が未定義です (config/checklist.yaml)</p>
      ) : (
        <div className="scroll flex min-h-0 flex-1 flex-col">
          {items.map((item, i) => {
            const isNext = i === nextIndex && !locked;
            return (
              <label
                key={item.id}
                className={cx(
                  "flex cursor-pointer items-center gap-3 border-l-2 border-transparent px-2 py-[0.5rem] text-[1.05em]",
                  "hover:bg-base-200",
                  // 次に唱える 1 項目だけを強調する。全部強調すると強調にならない
                  isNext && "border-l-warning bg-base-200 font-medium",
                  item.checked && "text-base-content/45",
                )}
              >
                <input
                  type="checkbox"
                  className="checkbox shrink-0 checkbox-sm checked:border-success checked:bg-success checked:text-success-content"
                  // 行内に「次」バッジなどの装飾を置くため、読み上げ名は項目名に固定する
                  aria-label={item.label}
                  checked={item.checked}
                  disabled={locked}
                  onChange={(e) =>
                    setChecklistItem(checklistRole, item.id, e.currentTarget.checked)
                  }
                />
                <span className={cx("min-w-0 flex-1", item.checked && "line-through")}>
                  {item.label}
                </span>
                {isNext ? (
                  <span className="shrink-0 text-[0.85em] whitespace-nowrap text-warning">次</span>
                ) : null}
              </label>
            );
          })}
        </div>
      )}
    </Panel>
  );
}
