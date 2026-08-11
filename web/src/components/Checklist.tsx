import { Button, Checkbox } from "@tsaito18/tuicss-react";

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
    <div className="tui-window" style={{ display: "flex", flexDirection: "column", minHeight: 0 }}>
      <fieldset
        className="tui-fieldset"
        style={{ display: "flex", flexDirection: "column", flex: 1, minHeight: 0 }}
      >
        <legend>
          <span className={completed ? "green-255-text" : "yellow-255-text"}>
            {completed ? "[✓]" : "[ ]"} {title}
          </span>
        </legend>

        <div style={{ display: "flex", justifyContent: "space-between", marginBottom: "0.5rem" }}>
          <span style={{ opacity: 0.8 }}>
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
          <p style={{ opacity: 0.7 }}>チェック項目が未定義です (config/checklist.yaml)</p>
        ) : (
          <div
            style={{ display: "flex", flexDirection: "column", gap: "0.25rem", overflowY: "auto" }}
          >
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
          <p className="green-255-text" style={{ marginTop: "0.5rem" }}>
            ✓ 指差喚呼 完了
          </p>
        ) : null}
      </fieldset>
    </div>
  );
}
