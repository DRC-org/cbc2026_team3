import { memo } from "react";

import { cx } from "@/lib/cx";
import type { ChecklistItem } from "@/lib/protocol";

interface ChecklistItemsProps {
  items: readonly ChecklistItem[];
  nextId: string | null;
  locked: boolean;
  onToggle: (itemId: string, checked: boolean) => void;
  className?: string;
}

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
              isNext && "border-l-warning bg-base-200 font-medium",
              item.checked && "text-base-content/45",
            )}
          >
            <input
              type="checkbox"
              className="checkbox shrink-0 checkbox-sm checked:border-success checked:bg-success checked:text-success-content"
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
