import { Button, Checkbox, Fieldset } from "@tsaito18/tuicss-react";

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
    <Fieldset
      className="panel panel-fill"
      legend={
        <span className={completed ? "success-text" : "warning-text"}>
          {completed ? "[✓]" : "[ ]"} {title}
        </span>
      }
    >
      <div className="hsplit no-shrink" style={{ alignItems: "center" }}>
        <span className="dim">
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
        <p className="dim" style={{ marginTop: "0.5rem" }}>
          チェック項目が未定義です (config/checklist.yaml)
        </p>
      ) : (
        <div className="panel-body scroll" style={{ gap: "0.25rem", marginTop: "0.5rem" }}>
          {items.map((item) => (
            <Checkbox
              key={item.id}
              checked={item.checked}
              disabled={locked}
              onChange={(e) => setChecklistItem(checklistRole, item.id, e.currentTarget.checked)}
            >
              {item.label}
            </Checkbox>
          ))}
        </div>
      )}

      {completed ? (
        <p className="success-text no-shrink" style={{ marginTop: "0.5rem" }}>
          ✓ 指差喚呼 完了
        </p>
      ) : null}
    </Fieldset>
  );
}
