import { Check, Info, RotateCcw, Zap } from "lucide-react";
import { memo } from "react";

import { HomingButtons } from "@/components/homing/HomingButtons";
import { HomingPanel } from "@/components/homing/HomingPanel";
import { SwitchMeasurePanel } from "@/components/homing/SwitchMeasurePanel";
import { ChecklistItems } from "@/components/monitor/ChecklistItems";
import { MotorCheckButton } from "@/components/motorcheck/MotorCheckButton";
import { MotorCheckPanel } from "@/components/motorcheck/MotorCheckPanel";
import { MotorCheckSummary } from "@/components/motorcheck/MotorCheckSummary";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { Panel } from "@/components/ui/Panel";
import { ScrollArea } from "@/components/ui/ScrollArea";
import { Section } from "@/components/ui/Section";
import { useRobotCommands, useRobotStatus } from "@/context/RobotContext";
import type { ChecklistGroup } from "@/lib/checklistGroups";
import {
  CHECKLIST_GROUP_TITLE,
  groupChecklistItems,
  nextChecklistItemId,
} from "@/lib/checklistGroups";
import { cx } from "@/lib/cx";
import { isDuringMatch, isSetupPhase } from "@/lib/phase";
import type { ChecklistItem, MatchCourt } from "@/lib/protocol";
import { CHECKLIST_ROLE, MALFORMED } from "@/lib/protocol";
import { TONE_PROGRESS_CLASS } from "@/lib/tone";

const TITLE = "試合準備";

const COURT_OPTIONS: { value: MatchCourt; label: string; selectedClass: string }[] = [
  { value: "red", label: "赤コート", selectedClass: "border-error bg-error text-error-content" },
  { value: "blue", label: "青コート", selectedClass: "border-info bg-info text-info-content" },
];

function GroupProgress({ items }: { items: readonly ChecklistItem[] }) {
  const done = items.filter((i) => i.checked).length;
  if (items.length === 0) return null;
  return (
    <span
      className={cx(
        "font-mono tabular-nums",
        done === items.length ? "text-success" : "text-base-content/70",
      )}
    >
      {done}/{items.length}
    </span>
  );
}

export const MatchPrep = memo(function MatchPrep({
  onRequestReset,
}: {
  onRequestReset: () => void;
}) {
  const { matchState, serverInfo, connected } = useRobotStatus();
  const { setChecklistItem, checkAllChecklist, setCourt } = useRobotCommands();
  const { court, phase } = matchState;

  const checklists = matchState.checklists;
  const unreadable = checklists === MALFORMED;
  const checklist = unreadable ? undefined : checklists[CHECKLIST_ROLE];
  const items = checklist?.items ?? [];

  const locked = !isSetupPhase(phase) || !connected;
  const courtLocked = isDuringMatch(phase) || !connected;

  const checkedCount = items.filter((i) => i.checked).length;
  const completed = checklist?.completed ?? false;
  const percent = items.length > 0 ? (checkedCount / items.length) * 100 : 0;
  const nextId = nextChecklistItemId(items);
  const grouped = groupChecklistItems(items);

  const itemsOf = (group: ChecklistGroup) => (
    <ChecklistItems
      items={grouped[group]}
      nextId={nextId}
      locked={locked}
      onToggle={(itemId, checked) => setChecklistItem(CHECKLIST_ROLE, itemId, checked)}
      className="-mx-2"
    />
  );

  return (
    <Panel
      legend={TITLE}
      className="min-h-0"
      bodyClassName="p-0"
      actions={
        <>
          {serverInfo.dev_tools ? (
            <Button
              tone="warn"
              disabled={locked || completed}
              onClick={() => checkAllChecklist(CHECKLIST_ROLE)}
              aria-label="指差喚呼を開発用に全てチェック"
            >
              <Icon as={Zap} />
              DEV 全チェック
            </Button>
          ) : null}
          <Button
            disabled={locked || (!unreadable && checkedCount === 0)}
            onClick={onRequestReset}
            aria-label="指差喚呼をリセットしてセッティングタイムへ戻す"
          >
            <Icon as={RotateCcw} />
            RESET
          </Button>
        </>
      }
    >
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
        ) : null}
      </div>

      <ScrollArea className="gap-1.5 px-2 py-1.5">
        {unreadable ? (
          <p className="text-error">
            指差喚呼の配信を読めていません。進捗を画面から確認できません
            (サーバーのログを確認してください)
          </p>
        ) : items.length === 0 ? (
          <p className="text-base-content/70">チェック項目が未定義です (config/checklist.yaml)</p>
        ) : null}

        {grouped.preflight.length > 0 ? (
          <Section
            title={CHECKLIST_GROUP_TITLE.preflight}
            aside={<GroupProgress items={grouped.preflight} />}
          >
            {itemsOf("preflight")}
          </Section>
        ) : null}

        <Section
          title={CHECKLIST_GROUP_TITLE.court}
          aside={<GroupProgress items={grouped.court} />}
        >
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <div className="join">
              {COURT_OPTIONS.map((opt) => (
                <Button
                  key={opt.value}
                  className={cx("join-item", court === opt.value && opt.selectedClass)}
                  disabled={courtLocked}
                  onClick={() => setCourt(opt.value)}
                  aria-pressed={court === opt.value}
                >
                  {opt.label}
                </Button>
              ))}
            </div>
            <p className="flex items-center gap-1.5 text-[0.9em] text-base-content/70">
              <Icon as={Info} />
              変更するとチェックリストは全てリセットされます
            </p>
          </div>
          {itemsOf("court")}
        </Section>

        <Section
          title={CHECKLIST_GROUP_TITLE.motor_check}
          aside={
            <span className="flex items-center gap-2">
              <MotorCheckSummary />
              <GroupProgress items={grouped.motor_check} />
            </span>
          }
        >
          <div className="flex flex-wrap items-center gap-2">
            <MotorCheckButton />
          </div>
          <MotorCheckPanel />
          <div className="flex flex-wrap items-center gap-2">
            <HomingButtons />
          </div>
          <HomingPanel />
          <SwitchMeasurePanel />
          {itemsOf("motor_check")}
        </Section>

        {grouped.other.length > 0 ? (
          <Section
            title={CHECKLIST_GROUP_TITLE.other}
            aside={<GroupProgress items={grouped.other} />}
          >
            {itemsOf("other")}
          </Section>
        ) : null}

        {grouped.final.length > 0 ? (
          <Section
            title={CHECKLIST_GROUP_TITLE.final}
            aside={<GroupProgress items={grouped.final} />}
          >
            {itemsOf("final")}
          </Section>
        ) : null}
      </ScrollArea>
    </Panel>
  );
});
