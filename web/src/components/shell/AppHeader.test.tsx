import { act, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AppHeader } from "@/components/shell/AppHeader";
import type { MatchState } from "@/lib/protocol";
import { MALFORMED } from "@/lib/protocol";
import type { RobotContextValue } from "@/test/robotContext";
import { DEFAULT_MATCH_STATE, renderWithRobot } from "@/test/robotContext";

/** タブ帯の描画回数を数える。ヘッダー本体が描き直されたときだけ増える */
const counts = vi.hoisted(() => ({ tabBar: 0 }));

vi.mock("@/components/shell/TabBar", () => ({
  TabBar: () => {
    counts.tabBar += 1;
    return null;
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

/**
 * フェーズチップとコートチップは `Record` の索引で描く。未知の値が素通しで入ると
 * 索引が `undefined` になり、`StatusBadge` がクラス無し・文字無しで描かれて
 * **帯からチップごと消える**。コートは「誤設定のまま試合に入る事故を防ぐため
 * 常時表示している」要素なので、消えたことに誰も気付けない。
 */
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
    // ヘッダーは RouteErrorBoundary の外。ここが描けなくなると止める手段が消える
    mount({ phase: MALFORMED, court: MALFORMED });

    expect(screen.getByRole("button", { name: "緊急停止" })).toBeInTheDocument();
  });
});

describe("AppHeader の接続表示", () => {
  it("接続表示そのものが接続先設定の入口になっている", async () => {
    // 繋がらないときに操縦者が最初に見る場所。ここが入口でないと、
    // 接続先を変える手段が平常時の画面から消える
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

/**
 * 時計は毎秒 `setState` する。ヘッダー本体へインライン展開すると、タブ帯を含む
 * ヘッダー全体が 1 秒ごとに描き直される（試合中ずっと続く）。
 * 独立したコンポーネントに保つことでその更新を時計の中へ閉じ込める。
 */
describe("ヘッダーの時計", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("秒が進んでもヘッダー本体を描き直さない", () => {
    vi.setSystemTime(new Date(2026, 0, 1, 19, 5, 50));
    mount();
    const before = counts.tabBar;
    const shown = screen.getByText(/^\d{1,2}:\d{2}:\d{2}$/).textContent;

    act(() => vi.advanceTimersByTime(3000));

    // 時計自身は進んでいる（進んでいなければ「描き直さない」は自明で無意味）
    expect(screen.getByText(/^\d{1,2}:\d{2}:\d{2}$/).textContent).not.toBe(shown);
    expect(counts.tabBar).toBe(before);
  });
});
