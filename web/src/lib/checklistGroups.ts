import type { ChecklistItem } from "@/lib/protocol";

export const CHECKLIST_GROUPS = ["preflight", "court", "motor_check", "other", "final"] as const;

export type ChecklistGroup = (typeof CHECKLIST_GROUPS)[number];

export const CHECKLIST_GROUP_TITLE: Record<ChecklistGroup, string> = {
  preflight: "通電前・機体の初期状態",
  court: "コート設定",
  motor_check: "アクチュエータ動作確認",
  other: "その他の確認",
  final: "開始直前",
};

const KNOWN: ReadonlySet<string> = new Set<string>(CHECKLIST_GROUPS);

export type GroupedChecklist = Record<ChecklistGroup, ChecklistItem[]>;

export function groupChecklistItems(items: readonly ChecklistItem[]): GroupedChecklist {
  const grouped = Object.fromEntries(
    CHECKLIST_GROUPS.map((g) => [g, [] as ChecklistItem[]]),
  ) as GroupedChecklist;
  for (const item of items) {
    const group = item.group && KNOWN.has(item.group) ? (item.group as ChecklistGroup) : "other";
    grouped[group].push(item);
  }
  return grouped;
}

export function nextChecklistItemId(items: readonly ChecklistItem[]): string | null {
  return items.find((item) => !item.checked)?.id ?? null;
}
