import { NavLink, useLocation } from "react-router";

import { Kbd } from "@/components/ui/Kbd";
import { useRobot } from "@/context/RobotContext";
import type { RobotState } from "@/hooks/useRobotSocket";
import { cx } from "@/lib/cx";
import { TABS } from "@/lib/tabs";
import type { Tone } from "@/lib/tone";
import { TONE_STATUS_CLASS } from "@/lib/tone";

/**
 * タブに出す注意喚起インジケータ。
 * 操縦者は自分のタブに張り付くため、他機がトリガー待ちや異常状態でも気付けない。
 * 切り替えなくても異変が分かるよう、タブラベル側に状態を出す。
 */
function tabTone(state: RobotState | undefined): Tone | null {
  if (!state) return null;
  if (state.health && state.health.overall !== "ok") return "error";
  if (state.waiting_trigger) return "warning";
  return null;
}

export function TabBar() {
  const { states } = useRobot();
  // ?ws= による接続先の上書きはロード時にしか読まれない。リンクで search を捨てると
  // 「タブを切り替えてリロードしたら接続先が既定へ戻る」事故になるため引き継ぐ
  const { search } = useLocation();

  return (
    <nav className="tabs tabs-box shrink-0 bg-base-200 p-[0.15rem] tabs-sm" aria-label="画面切替">
      {TABS.map((tab) => {
        const tone = tab.robotKey ? tabTone(states[tab.robotKey]) : null;
        return (
          <NavLink
            key={tab.path}
            to={{ pathname: tab.path, search }}
            className={({ isActive }) =>
              cx(
                "tab gap-1.5 px-2 text-base-content/70",
                isActive && "tab-active font-medium text-base-content",
              )
            }
          >
            <Kbd className="bg-base-100">{tab.hotkey}</Kbd>
            <span>{tab.label}</span>
            {/* 面積が小さいぶん、無印との差が付くよう色付きの LED だけを出す */}
            {tone ? (
              <span
                className={TONE_STATUS_CLASS[tone]}
                aria-label={tone === "error" ? "異常あり" : "許可待ち"}
                role="img"
              />
            ) : null}
          </NavLink>
        );
      })}
    </nav>
  );
}
