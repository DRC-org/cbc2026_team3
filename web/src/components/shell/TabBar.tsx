import { memo, useRef } from "react";
import { NavLink, useLocation } from "react-router";

import { Kbd } from "@/components/ui/Kbd";
import { useRobotStates, useRobotStatus } from "@/context/RobotContext";
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

interface TabIndicator {
  tone: Tone;
  label: string;
}

/**
 * タブに出す注意喚起インジケータ。
 * 操縦者は自分のタブに張り付くため、他機がトリガー待ちや異常状態でも気付けない。
 * 切り替えなくても異変が分かるよう、タブラベル側に状態を出す。
 *
 * 健全性の判定は `evaluateHealth` に委ねる。ここで `overall !== "ok"` と書いていた頃は
 * degraded まで異常 (赤 LED) 扱いになり、同じ瞬間に Monitor は黄「要確認」を出していた。
 *
 * **切断中は灰の LED へ倒す。** 手元にあるのは切れた瞬間の値なので、消灯 (異常なし)
 * として出すと凍った判定を今の機体の状態として読ませることになる。
 */
function tabIndicator(state: RobotState | undefined, connected: boolean): TabIndicator | null {
  if (!connected) return { tone: "neutral", label: "通信断" };
  if (!state) return null;
  const verdict = evaluateHealth(state.health, state.safety, connected);
  if (verdict.tone === "error") return { tone: "error", label: TONE_LABEL.error };
  // 操縦者の操作を待っている方が行動に直結するため、同じ警告レベルでは先に出す
  if (state.waiting_trigger) return { tone: "warning", label: "許可待ち" };
  if (verdict.tone === "warning") return { tone: "warning", label: TONE_LABEL.warning };
  return null;
}

/** 同じ内容なら同じ参照を返すための署名。LED は tone とラベルしか描かない */
function signatureOf(indicators: (TabIndicator | null)[]): string {
  return indicators.map((i) => (i ? `${i.tone}:${i.label}` : "-")).join("|");
}

/**
 * LED の内容だけを購読して畳む。
 *
 * ここは 20Hz × 2 台のテレメトリで毎秒 40 回動くが、**返す値は滅多に変わらない**
 * (LED が要るのは異常時と許可待ちだけ)。内容が同じ間は同じ配列参照を返すので、
 * これを props で受ける `TabBarNav` (memo) は再描画ごと止まる。
 * ここで畳まずにテレメトリを下へ流すと、外枠を memo で切り離した意味が消えて
 * タブ帯が試合中ずっと 40Hz で描き直される。
 */
function useTabIndicators(): (TabIndicator | null)[] {
  const states = useRobotStates();
  const { connected } = useRobotStatus();

  const next = TABS.map((tab) =>
    tab.robotKey ? tabIndicator(states[tab.robotKey], connected) : null,
  );
  const signature = signatureOf(next);
  const stable = useRef(next);
  const stableSignature = useRef(signature);
  if (stableSignature.current !== signature) {
    stableSignature.current = signature;
    stable.current = next;
  }
  return stable.current;
}

/**
 * タブ帯の描画。**テレメトリを直接読まない。**
 *
 * 読むのは畳んだ LED と現在の location だけなので、memo が実際に効く
 * (props にテレメトリ由来の値を足した瞬間に無効になる)。
 */
const TabBarNav = memo(function TabBarNav({ indicators }: { indicators: (TabIndicator | null)[] }) {
  // ?ws= による接続先の上書きはロード時にしか読まれない。リンクで search を捨てると
  // 「タブを切り替えてリロードしたら接続先が既定へ戻る」事故になるため引き継ぐ
  const { search } = useLocation();

  return (
    <nav className="tabs tabs-box shrink-0 bg-base-200 p-[0.15rem] tabs-sm" aria-label="画面切替">
      {TABS.map((tab, i) => {
        const indicator = indicators[i];
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
});

export function TabBar() {
  const indicators = useTabIndicators();
  return <TabBarNav indicators={indicators} />;
}
