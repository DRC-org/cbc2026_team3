import { act, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AppHeader } from "@/components/shell/AppHeader";
import type { MatchState } from "@/lib/protocol";
import { MALFORMED } from "@/lib/protocol";
import type { RobotContextValue } from "@/test/robotContext";
import { DEFAULT_MATCH_STATE, renderWithRobot } from "@/test/robotContext";

const counts = vi.hoisted(() => ({ tabBar: 0 }));

vi.mock("@/components/shell/TabBar", () => ({
  TabBar: () => {
    counts.tabBar += 1;
    return <div data-testid="tab-bar" />;
  },
}));

beforeEach(() => {
  counts.tabBar = 0;
});

function mount(over: Partial<MatchState> = {}, context: Partial<RobotContextValue> = {}) {
  return renderWithRobot(
    <MemoryRouter>
      <AppHeader />
    </MemoryRouter>,
    { matchState: { ...DEFAULT_MATCH_STATE, ...over }, ...context },
  );
}

function precedes(earlier: Element, later: Element): boolean {
  return Boolean(earlier.compareDocumentPosition(later) & Node.DOCUMENT_POSITION_FOLLOWING);
}

describe("AppHeader のフェーズ・コート表示", () => {
  it("既知の値はそのまま出す", () => {
    mount({ phase: "match", court: "blue" });

    expect(screen.getByText("試合中")).toBeInTheDocument();
    expect(screen.getByText("青コート")).toBeInTheDocument();
  });

  it("読めなかったフェーズを空白にせず「不明」として出す", () => {
    mount({ phase: MALFORMED });

    expect(screen.getByText("フェーズ不明")).toBeInTheDocument();
  });

  it("読めなかったコートを空白にせず「不明」として出す", () => {
    mount({ court: MALFORMED });

    expect(screen.getByText("コート不明")).toBeInTheDocument();
  });

  it("緊急停止は何があっても押せる位置に残る", () => {
    mount({ phase: MALFORMED, court: MALFORMED });

    expect(screen.getByRole("button", { name: "緊急停止" })).toBeInTheDocument();
  });
});

describe("AppHeader の緊急停止まわりの配置", () => {
  const eStop = () => screen.getByRole("button", { name: "緊急停止" });
  const connection = () => screen.getByRole("button", { name: /Connected/ });

  it("押下可能な要素は EMG STOP より手前に置く", () => {
    const { container } = mount();
    const buttons = Array.from(container.querySelectorAll("button"));

    expect(buttons.length).toBeGreaterThan(1);
    expect(buttons.at(-1)).toBe(eStop());
  });

  it("EMG STOP の直左には押せない要素を置く", () => {
    mount();

    expect(precedes(connection(), screen.getByText("セッティングタイム"))).toBe(true);
    expect(precedes(connection(), screen.getByText("赤コート"))).toBe(true);
  });

  it("タブ帯はヘッダーの最左に置く", () => {
    mount();
    const tabBar = screen.getByTestId("tab-bar");

    expect(precedes(tabBar, connection())).toBe(true);
    expect(precedes(tabBar, screen.getByText("セッティングタイム"))).toBe(true);
    expect(precedes(tabBar, eStop())).toBe(true);
  });

  it("ヘッダーの EMG STOP は点滅させない", () => {
    const { container } = mount();

    expect(eStop()).toBeInTheDocument();
    expect(container.querySelector(".alert-blink")).toBeNull();
  });
});

describe("AppHeader の接続表示", () => {
  it("接続表示そのものが接続先設定の入口になっている", async () => {
    const { context } = mount();

    await userEvent.click(screen.getByRole("button", { name: /Connected/ }));

    expect(context.openWsSettings).toHaveBeenCalled();
  });

  it("切断中は接続先とともに切断を出す", () => {
    mount({}, { connected: false, wsUrl: "ws://drc:8080/ws" });

    expect(screen.getByRole("button", { name: /Disconnected/ })).toBeInTheDocument();
    expect(screen.getByText("drc:8080")).toBeInTheDocument();
  });
});

describe("ヘッダーの時計", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("秒が進んでもヘッダー本体を描き直さない", () => {
    vi.setSystemTime(new Date(2026, 0, 1, 19, 5, 50));
    mount();
    const before = counts.tabBar;
    const shown = screen.getByText(/^\d{1,2}:\d{2}:\d{2}$/).textContent;

    act(() => vi.advanceTimersByTime(3000));

    expect(screen.getByText(/^\d{1,2}:\d{2}:\d{2}$/).textContent).not.toBe(shown);
    expect(counts.tabBar).toBe(before);
  });
});
