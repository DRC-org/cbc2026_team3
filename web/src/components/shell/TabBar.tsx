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

function tabIndicator(state: RobotState | undefined, connected: boolean): TabIndicator | null {
  if (!connected) return { tone: "neutral", label: "通信断" };
  if (!state) return null;
  const verdict = evaluateHealth(state.health, state.safety, connected);
  if (verdict.tone === "error") return { tone: "error", label: TONE_LABEL.error };
  if (state.waiting_trigger) return { tone: "warning", label: "許可待ち" };
  if (verdict.tone === "warning") return { tone: "warning", label: TONE_LABEL.warning };
  return null;
}

function signatureOf(indicators: (TabIndicator | null)[]): string {
  return indicators.map((i) => (i ? `${i.tone}:${i.label}` : "-")).join("|");
}

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

const TabBarNav = memo(function TabBarNav({ indicators }: { indicators: (TabIndicator | null)[] }) {
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
