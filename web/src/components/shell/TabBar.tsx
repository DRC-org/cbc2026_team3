import { NavLink, useLocation } from "react-router";

import { Kbd } from "@/components/ui/Kbd";
import { useRobotStates } from "@/context/RobotContext";
import { cx } from "@/lib/cx";
import { evaluateHealth } from "@/lib/healthVerdict";
import type { RobotState } from "@/lib/protocol";
import { TABS } from "@/lib/tabs";
import type { Tone } from "@/lib/tone";
import { TONE_STATUS_CLASS } from "@/lib/tone";

/** LED は面積が小さいので、ラベルは読み上げ・テストのために対で持つ */
const TONE_LABEL: Record<Tone, string> = {
  error: "異常あり",
  warning: "要確認",
  success: "",
  info: "",
  neutral: "",
};

/**
 * タブに出す注意喚起インジケータ。
 * 操縦者は自分のタブに張り付くため、他機がトリガー待ちや異常状態でも気付けない。
 * 切り替えなくても異変が分かるよう、タブラベル側に状態を出す。
 *
 * 健全性の判定は `evaluateHealth` に委ねる。ここで `overall !== "ok"` と書いていた頃は
 * degraded まで異常 (赤 LED) 扱いになり、同じ瞬間に Monitor は黄「要確認」を出していた。
 */
function tabIndicator(state: RobotState | undefined): { tone: Tone; label: string } | null {
  if (!state) return null;
  const verdict = evaluateHealth(state.health, state.safety);
  if (verdict.tone === "error") return { tone: "error", label: TONE_LABEL.error };
  // 操縦者の操作を待っている方が行動に直結するため、同じ警告レベルでは先に出す
  if (state.waiting_trigger) return { tone: "warning", label: "許可待ち" };
  if (verdict.tone === "warning") return { tone: "warning", label: TONE_LABEL.warning };
  return null;
}

export function TabBar() {
  const states = useRobotStates();
  // ?ws= による接続先の上書きはロード時にしか読まれない。リンクで search を捨てると
  // 「タブを切り替えてリロードしたら接続先が既定へ戻る」事故になるため引き継ぐ
  const { search } = useLocation();

  return (
    <nav className="tabs tabs-box shrink-0 bg-base-200 p-[0.15rem] tabs-sm" aria-label="画面切替">
      {TABS.map((tab) => {
        const indicator = tab.robotKey ? tabIndicator(states[tab.robotKey]) : null;
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
            {indicator ? (
              <span
                className={TONE_STATUS_CLASS[indicator.tone]}
                aria-label={indicator.label}
                role="img"
              />
            ) : null}
          </NavLink>
        );
      })}
    </nav>
  );
}
