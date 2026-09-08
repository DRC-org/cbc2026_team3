import type { ChecklistItem } from "@/lib/protocol";

/**
 * 指差喚呼の項目を「画面のどこに置くか」で仕分ける。
 *
 * 仕分けの正は `config/checklist.yaml` の `group` で、ここが持つのは**その語彙と画面上の
 * 並び順だけ**。項目名も id もここには書かない (config を直せば画面が追従する)。
 *
 * **どの項目も必ずどこかの群に入る。** 未指定・未知の group は `other` へ落ちるが、
 * 描かれないことはない —— 未知を捨てると、config に group を書き足した瞬間にその項目が
 * 画面から消え、指差喚呼が 1 つ足りないまま試合開始のゲートだけが開かない。
 */
export const CHECKLIST_GROUPS = ["preflight", "court", "motor_check", "other", "final"] as const;

export type ChecklistGroup = (typeof CHECKLIST_GROUPS)[number];

/** 対応するコントロールを持たない群の見出し。持つ群の見出しはコントロール側が兼ねる。 */
export const CHECKLIST_GROUP_TITLE: Record<ChecklistGroup, string> = {
  preflight: "通電前・機体の初期状態",
  court: "コート設定",
  motor_check: "アクチュエータ動作確認",
  other: "その他の確認",
  final: "開始直前",
};

const KNOWN: ReadonlySet<string> = new Set<string>(CHECKLIST_GROUPS);

export type GroupedChecklist = Record<ChecklistGroup, ChecklistItem[]>;

/**
 * 項目を群ごとに分ける。**配信順は群の中で保つ** (指差喚呼は上から順に唱える運用で、
 * config の並びがそのまま読み上げ順になっている)。
 */
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

/**
 * 次に唱える 1 項目の id。**画面全体で 1 つだけ**返す。
 *
 * 群ごとに「次」を出すと強調が 5 つ並び、強調でなくなる。未完の先頭を配信順で選ぶ
 * ことで、群をまたいでも読み上げ順のとおりに 1 つずつ進む。
 */
export function nextChecklistItemId(items: readonly ChecklistItem[]): string | null {
  return items.find((item) => !item.checked)?.id ?? null;
}
