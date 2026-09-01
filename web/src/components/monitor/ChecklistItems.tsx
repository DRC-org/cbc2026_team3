import { memo } from "react";

import { cx } from "@/lib/cx";
import type { ChecklistItem } from "@/lib/protocol";

interface ChecklistItemsProps {
  items: readonly ChecklistItem[];
  /**
   * 強調する 1 項目の id。**画面全体で 1 つ**（`nextChecklistItemId` が決める）。
   * 群ごとに「次」を出すと強調が 5 つ並び、強調でなくなる。
   */
  nextId: string | null;
  locked: boolean;
  onToggle: (itemId: string, checked: boolean) => void;
  /** 行のホバー面を区分の左右余白いっぱいへ広げるための打ち消し (`-mx-2`) */
  className?: string;
}

/**
 * 指差喚呼の項目行。**どの群でも同じ見た目で描く唯一の場所**。
 *
 * 群ごとに行を書き分けると、コート設定の下の 1 行と動作確認の下の 12 行で
 * チェックボックスの大きさや打ち消し線の有無が食い違い、同じ操作に見えなくなる。
 *
 * 「どの項目をどこへ置くか」はここでは決めない（`lib/checklistGroups.ts`）。
 * ここが持つのは 1 行の描き方と、上から順に唱えていく運用のための強調だけ。
 */
export const ChecklistItems = memo(function ChecklistItems({
  items,
  nextId,
  locked,
  onToggle,
  className,
}: ChecklistItemsProps) {
  return (
    <div className={cx("flex flex-col", className)}>
      {items.map((item) => {
        const isNext = item.id === nextId && !locked;
        return (
          <label
            key={item.id}
            className={cx(
              "flex cursor-pointer items-center gap-3 border-l-2 border-transparent px-2 py-[0.4rem] text-[1.05em]",
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
              onChange={(e) => onToggle(item.id, e.currentTarget.checked)}
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
  );
});
