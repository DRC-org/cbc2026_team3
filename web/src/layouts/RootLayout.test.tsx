import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider, createMemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { installMockWebSocket, latestSocket } from "@/test/mockWebSocket";

/**
 * 外枠 (RootLayout) がテレメトリで再描画されないことを、実際の WS 受信経路で確かめる。
 *
 * サーバーは 50ms 間隔で state を配信する。外枠まで巻き込んで再描画していると、
 * ステータスバー・トースト・接続バナーが毎秒 40 回描き直されることになる。
 * ここでは外枠の部品 (StatusBar) とテレメトリ購読者 (TabBar) を数える差し替えに
 * して、「テレメトリを読む者だけが動く」ことを固定する。
 */

const counts = vi.hoisted(() => ({ statusBar: 0, tabBar: 0, checklist: 0 }));

vi.mock("@/components/shell/StatusBar", () => ({
  StatusBar: () => {
    counts.statusBar += 1;
    return null;
  },
}));

vi.mock("@/components/shell/TabBar", async () => {
  const { useRobotStates } = await import("@/context/RobotContext");
  return {
    TabBar: () => {
      // 本物と同じくテレメトリを購読する。こちらは配信ごとに動くのが正しい
      useRobotStates();
      counts.tabBar += 1;
      return null;
    },
  };
});

/**
 * 指差喚呼リストは memo で切り離してある (試合状態しか読まない)。差し替えも memo に
 * しておくと、切り離しを壊すのは「親が毎描画 新しい props を渡す」場合だけになる。
 */
vi.mock("@/components/operator/Checklist", async () => {
  const { memo } = await import("react");
  return {
    Checklist: memo(function Checklist() {
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
  counts.statusBar = 0;
  counts.tabBar = 0;
  counts.checklist = 0;
  installMockWebSocket();
});

describe("RootLayout のテレメトリ再描画", () => {
  it("state 配信で外枠を再描画しない (購読していない部品は動かない)", async () => {
    await renderApp();
    act(() => latestSocket().open());

    const shellBefore = counts.statusBar;
    const tabsBefore = counts.tabBar;

    for (let i = 0; i < 20; i++) {
      act(() => latestSocket().receive(stateMessage(i)));
    }

    expect(counts.statusBar).toBe(shellBefore);
    // 購読側は届いた回数ぶん更新される (止まっていたら値が凍る)
    expect(counts.tabBar).toBeGreaterThan(tabsBefore);
  });

  it("操縦者画面の指差喚呼リストをテレメトリで描き直さない", async () => {
    // 準備フェーズの主役。8 行のチェックリストを毎秒 40 回描き直す理由は無い
    await renderApp("/main-hand");
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

/**
 * 切断中の緊急停止。`send` は readyState を見て黙って捨てるので、その後で
 * 無条件に楽観的更新をすると「何も送っていないのに全画面が赤い停止オーバーレイ」
 * になる。しかもオーバーレイは inset:0 で、矛盾を示すはずの接続バナーを覆い隠す。
 */
describe("切断中の緊急停止", () => {
  it("送れていないのに停止した体裁を作らない", async () => {
    await renderApp();
    // open() を呼ばない = 切断中

    await userEvent.click(screen.getByRole("button", { name: "緊急停止" }));

    expect(latestSocket().sent).toHaveLength(0);
    expect(screen.queryByText("ALL MOTION HALTED")).toBeNull();
  });

  it("押したことと送れなかったことを操縦者へ伝える", async () => {
    // 「押したのに何も起きない」も同じくらい危険。黙って捨ててはならない
    await renderApp();

    await userEvent.click(screen.getByRole("button", { name: "緊急停止" }));

    expect(screen.getByText(/緊急停止を送信できませんでした/)).toBeInTheDocument();
    // 機体が止まっていないことまで書く。次の一手 (物理の非常停止) が変わる
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
