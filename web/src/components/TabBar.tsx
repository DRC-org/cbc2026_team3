import { NavLink, useLocation } from "react-router";

import { useRobot } from "@/context/RobotContext";
import type { RobotState } from "@/hooks/useRobotSocket";
import { cx } from "@/lib/cx";
import { TABS } from "@/lib/tabs";

/**
 * タブに出す注意喚起バッジ。
 * 操縦者は自分のタブに張り付くため、他機がトリガー待ちや異常状態でも気付けない。
 * 切り替えなくても異変が分かるよう、タブラベル側に状態を出す。
 */
function tabBadge(state: RobotState | undefined): { symbol: string; className: string } | null {
  if (!state) return null;
  const health = state.health;
  if (health && health.overall !== "ok") {
    return { symbol: "⚠", className: "danger-text" };
  }
  if (state.waiting_trigger) {
    return { symbol: "!", className: "warning-text" };
  }
  return null;
}

export function TabBar() {
  const { states } = useRobot();
  // ?ws= による接続先の上書きはロード時にしか読まれない。リンクで search を捨てると
  // 「タブを切り替えてリロードしたら接続先が既定へ戻る」事故になるため引き継ぐ
  const { search } = useLocation();

  return (
    <nav className="tui-tabs" aria-label="画面切替">
      {TABS.map((tab) => {
        const badge = tab.robotKey ? tabBadge(states[tab.robotKey]) : null;
        return (
          <NavLink
            key={tab.path}
            to={{ pathname: tab.path, search }}
            className={({ isActive }) => cx("tui-tab", isActive && "active")}
          >
            <span className="key-hint" style={{ marginLeft: 0, marginRight: "0.4em" }}>
              {tab.hotkey}
            </span>
            {tab.label}
            {badge ? <span className={badge.className}> [{badge.symbol}]</span> : null}
          </NavLink>
        );
      })}
    </nav>
  );
}
