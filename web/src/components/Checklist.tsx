import { Button } from "@/components/ui/Button";
import { Panel } from "@/components/ui/Panel";
import { useRobot } from "@/context/RobotContext";
import type { ChecklistRole } from "@/hooks/useRobotSocket";

interface ChecklistProps {
  checklistRole: ChecklistRole;
  title: string;
}

/**
 * セッティングタイム中の指差喚呼チェックリスト。
 * 状態はサーバー保持なので、同じロールを複数画面で開いても進捗が共有される。
 */
export function Checklist({ checklistRole, title }: ChecklistProps) {
  const { matchState, setChecklistItem, resetChecklist } = useRobot();
  const checklist = matchState.checklists[checklistRole];
  const locked = matchState.phase === "match" || matchState.phase === "finished";

  const items = checklist?.items ?? [];
  const checkedCount = items.filter((i) => i.checked).length;
  const completed = checklist?.completed ?? false;

  return (
    <Panel
      className="flex-1"
      legend={
        <span className={completed ? "text-success" : "text-warning"}>
          {completed ? "[✓]" : "[ ]"} {title}
        </span>
      }
    >
      <div className="hsplit shrink-0 items-center">
        <span className="text-fg-dim">
          {checkedCount} / {items.length} 項目
        </span>
        <Button
          disabled={locked || checkedCount === 0}
          onClick={() => resetChecklist(checklistRole)}
          aria-label={`${title} のチェックをすべて解除`}
        >
          ↺ CLEAR
        </Button>
      </div>

      {items.length === 0 ? (
        <p className="mt-2 text-fg-dim">チェック項目が未定義です (config/checklist.yaml)</p>
      ) : (
        <div className="panel-body scroll mt-2 gap-1">
          {items.map((item) => (
            <label key={item.id} className="hstack cursor-pointer">
              <input
                type="checkbox"
                className="checkbox checkbox-sm checked:border-success checked:text-success"
                checked={item.checked}
                disabled={locked}
                onChange={(e) => setChecklistItem(checklistRole, item.id, e.currentTarget.checked)}
              />
              <span className={locked ? "text-fg-dim" : undefined}>{item.label}</span>
            </label>
          ))}
        </div>
      )}

      {completed ? <p className="mt-2 shrink-0 text-success">✓ 指差喚呼 完了</p> : null}
    </Panel>
  );
}
