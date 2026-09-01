import { describe, expect, it } from "vitest";

import { CHECKLIST_GROUPS, groupChecklistItems, nextChecklistItemId } from "@/lib/checklistGroups";
import type { ChecklistItem } from "@/lib/protocol";

function item(over: Partial<ChecklistItem> & { id: string }): ChecklistItem {
  return { label: over.id, checked: false, ...over };
}

describe("groupChecklistItems", () => {
  it("宣言された群へ振り分ける", () => {
    const grouped = groupChecklistItems([
      item({ id: "court", group: "court" }),
      item({ id: "check", group: "motor_check" }),
      item({ id: "power", group: "preflight" }),
      item({ id: "clear", group: "final" }),
    ]);

    expect(grouped.court.map((i) => i.id)).toEqual(["court"]);
    expect(grouped.motor_check.map((i) => i.id)).toEqual(["check"]);
    expect(grouped.preflight.map((i) => i.id)).toEqual(["power"]);
    expect(grouped.final.map((i) => i.id)).toEqual(["clear"]);
  });

  it("group を持たない項目も未知の group の項目も落とさない", () => {
    // 落とすと、config に group を書き足した瞬間に項目が画面から消え、
    // 指差喚呼が 1 つ足りないまま試合開始のゲートだけが開かない状態になる。
    // ベンチ設定 (config/bench/*) は group を 1 つも持たない
    const grouped = groupChecklistItems([
      item({ id: "bench" }),
      item({ id: "typo", group: "moter_check" }),
      item({ id: "null_group", group: null }),
    ]);

    expect(grouped.other.map((i) => i.id)).toEqual(["bench", "typo", "null_group"]);
  });

  it("群の中では配信順を保つ (config の並びがそのまま読み上げ順)", () => {
    const grouped = groupChecklistItems([
      item({ id: "b", group: "preflight" }),
      item({ id: "a", group: "preflight" }),
    ]);

    expect(grouped.preflight.map((i) => i.id)).toEqual(["b", "a"]);
  });

  it("どの群も必ずキーとして存在する (呼び出し側に undefined を出さない)", () => {
    const grouped = groupChecklistItems([]);
    for (const group of CHECKLIST_GROUPS) {
      expect(grouped[group]).toEqual([]);
    }
  });
});

describe("nextChecklistItemId", () => {
  it("未完の先頭を 1 つだけ返す (群をまたいでも読み上げ順)", () => {
    const next = nextChecklistItemId([
      item({ id: "done", checked: true, group: "preflight" }),
      item({ id: "here", group: "final" }),
      item({ id: "later", group: "court" }),
    ]);

    expect(next).toBe("here");
  });

  it("全て済んでいれば「次」は無い", () => {
    expect(nextChecklistItemId([item({ id: "a", checked: true })])).toBeNull();
  });
});
