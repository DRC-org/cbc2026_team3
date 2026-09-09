import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider, createMemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { installMockWebSocket, latestSocket } from "@/test/mockWebSocket";

const counts = vi.hoisted(() => ({ header: 0, tabBar: 0, checklist: 0 }));

const flags = vi.hoisted(() => ({ dashboardThrows: false }));

vi.mock("@/pages/Dashboard", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/pages/Dashboard")>();
  return {
    Dashboard: () => {
      if (flags.dashboardThrows) throw new Error("描画に失敗");
      return <actual.Dashboard />;
    },
  };
});

vi.mock("@/components/shell/Clock", () => ({
  Clock: () => {
    counts.header += 1;
    return null;
  },
}));

vi.mock("@/components/shell/TabBar", async () => {
  const { useRobotStates } = await import("@/context/RobotContext");
  return {
    TabBar: () => {
      useRobotStates();
      counts.tabBar += 1;
      return null;
    },
  };
});

vi.mock("@/components/monitor/MatchPrep", async () => {
  const { memo } = await import("react");
  return {
    MatchPrep: memo(function MatchPrep() {
      counts.checklist += 1;
      return null;
    }),
  };
});

function renderApp(path = "/monitor") {
  return import("@/routes").then(({ routes }) => {
    const router = createMemoryRouter(routes, { initialEntries: [path] });
    render(<RouterProvider router={router} />);
  });
}

function stateMessage(stepIndex: number) {
  return {
    type: "state",
    robot: "main_hand",
    step_index: stepIndex,
    motors: { lift: { pos: 0, vel: 0, torque: 0, temp: 40 + stepIndex * 0.1 } },
  };
}

beforeEach(() => {
  counts.header = 0;
  counts.tabBar = 0;
  counts.checklist = 0;
  flags.dashboardThrows = false;
  installMockWebSocket();
});

describe("RootLayout のテレメトリ再描画", () => {
  it("state 配信で外枠を再描画しない (購読していない部品は動かない)", async () => {
    await renderApp();
    act(() => latestSocket().open());

    const shellBefore = counts.header;
    const tabsBefore = counts.tabBar;
    expect(shellBefore).toBeGreaterThan(0);

    for (let i = 0; i < 20; i++) {
      act(() => latestSocket().receive(stateMessage(i)));
    }

    expect(counts.header).toBe(shellBefore);
    expect(counts.tabBar).toBeGreaterThan(tabsBefore);
  });

  it("Monitor の指差喚呼リストをテレメトリで描き直さない", async () => {
    await renderApp("/monitor");
    act(() => latestSocket().open());
    act(() => latestSocket().receive(stateMessage(0)));

    const before = counts.checklist;
    expect(before).toBeGreaterThan(0);

    for (let i = 1; i < 20; i++) {
      act(() => latestSocket().receive(stateMessage(i)));
    }

    expect(counts.checklist).toBe(before);
  });
});

describe("画面の描画例外", () => {
  let consoleError: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    flags.dashboardThrows = true;
    consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
  });

  afterEach(() => consoleError.mockRestore());

  it("落ちるのはタブの中身だけで、緊急停止ボタンは残る", async () => {
    await renderApp("/monitor");

    expect(screen.getByRole("button", { name: "緊急停止" })).toBeInTheDocument();
    expect(screen.getByText("この画面の描画に失敗しました")).toBeInTheDocument();
    expect(screen.getByText(/EMG STOP は生きています/)).toBeInTheDocument();
    expect(screen.getAllByRole("alert").length).toBeGreaterThan(1);
  });

  it("落ちた画面でも緊急停止を送れる", async () => {
    await renderApp("/monitor");
    act(() => latestSocket().open());

    await userEvent.click(screen.getByRole("button", { name: "緊急停止" }));

    expect(latestSocket().sent).toHaveLength(1);
  });

  it("緊急停止オーバーレイは境界の外なので出続ける", async () => {
    await renderApp("/monitor");
    act(() => latestSocket().open());
    act(() => latestSocket().receive({ type: "e_stop_state", active: true }));

    expect(screen.getByText("ALL MOTION HALTED")).toBeInTheDocument();
  });

  it("別のタブへ切り替えれば境界は解ける", async () => {
    await renderApp("/monitor");
    expect(screen.getByText("この画面の描画に失敗しました")).toBeInTheDocument();

    flags.dashboardThrows = false;
    await userEvent.keyboard("2");

    expect(screen.queryByText("この画面の描画に失敗しました")).toBeNull();
  });
});

describe("切断中の緊急停止", () => {
  it("送れていないのに停止した体裁を作らない", async () => {
    await renderApp();

    await userEvent.click(screen.getByRole("button", { name: "緊急停止" }));

    expect(latestSocket().sent).toHaveLength(0);
    expect(screen.queryByText("ALL MOTION HALTED")).toBeNull();
  });

  it("押したことと送れなかったことを操縦者へ伝える", async () => {
    await renderApp();

    await userEvent.click(screen.getByRole("button", { name: "緊急停止" }));

    expect(screen.getByText(/緊急停止を送信できませんでした/)).toBeInTheDocument();
    expect(screen.getByText(/停止していません/)).toBeInTheDocument();
  });

  it("切断中の Reset でオーバーレイを閉じない (機体側のラッチは残る)", async () => {
    await renderApp();
    act(() => latestSocket().open());
    act(() => latestSocket().receive({ type: "e_stop_state", active: true }));
    expect(screen.getByText("ALL MOTION HALTED")).toBeInTheDocument();

    act(() => latestSocket().close());
    await userEvent.click(screen.getByRole("button", { name: /Reset/ }));

    expect(screen.getByText("ALL MOTION HALTED")).toBeInTheDocument();
    expect(screen.getByText(/解除を送信できませんでした/)).toBeInTheDocument();
  });
});

describe("開発用の緊急停止オーバーレイ非表示", () => {
  const HIDE_BUTTON = "緊急停止ダイアログを開発用に非表示にする";
  const BANNER_TEXT = /EMERGENCY STOP — 全ロボット停止中/;

  async function hideOverlay() {
    await renderApp("/monitor");
    act(() => latestSocket().open());
    act(() => latestSocket().receive({ type: "server_info", dev_tools: true }));
    act(() => latestSocket().receive({ type: "e_stop_state", active: true }));
    expect(screen.getByText("ALL MOTION HALTED")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: HIDE_BUTTON }));

    expect(screen.queryByText("ALL MOTION HALTED")).toBeNull();
    expect(screen.getByText(BANNER_TEXT)).toBeInTheDocument();
  }

  it("非表示にしても帯の Reset から解除を送れる", async () => {
    await hideOverlay();

    await userEvent.click(screen.getByRole("button", { name: /Reset/ }));

    expect(latestSocket().sentJson()).toContainEqual({ type: "e_stop_release" });
  });

  it("解除して再びラッチしてもオーバーレイは戻らず帯で知らせる", async () => {
    await hideOverlay();

    act(() => latestSocket().receive({ type: "e_stop_state", active: false }));
    expect(screen.queryByText(BANNER_TEXT)).toBeNull();

    act(() => latestSocket().receive({ type: "e_stop_state", active: true }));

    expect(screen.queryByText("ALL MOTION HALTED")).toBeNull();
    expect(screen.getByText(BANNER_TEXT)).toBeInTheDocument();
  });

  it("dev_tools が落ちた server_info を受け直すとオーバーレイが戻る", async () => {
    await hideOverlay();

    act(() => latestSocket().receive({ type: "server_info", dev_tools: false }));

    expect(screen.getByText("ALL MOTION HALTED")).toBeInTheDocument();
    expect(screen.queryByText(BANNER_TEXT)).toBeNull();
  });

  it("切断中に帯の Reset を押しても解除した体裁を作らない", async () => {
    await hideOverlay();
    const sentBefore = latestSocket().sent.length;

    act(() => latestSocket().close());
    await userEvent.click(screen.getByRole("button", { name: /Reset/ }));

    expect(latestSocket().sent).toHaveLength(sentBefore);
    expect(screen.getByText(BANNER_TEXT)).toBeInTheDocument();
    expect(screen.getByText(/解除を送信できませんでした/)).toBeInTheDocument();
  });
});
