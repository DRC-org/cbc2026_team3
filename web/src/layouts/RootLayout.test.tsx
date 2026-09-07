import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider, createMemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { installMockWebSocket, latestSocket } from "@/test/mockWebSocket";

/**
 * 外枠 (RootLayout) がテレメトリで再描画されないことを、実際の WS 受信経路で確かめる。
 *
 * サーバーは 50ms 間隔で state を配信する。外枠まで巻き込んで再描画していると、
 * ヘッダー・トースト・接続バナーが毎秒 40 回描き直されることになる。
 * ここでは外枠の部品 (AppHeader) とテレメトリ購読者 (TabBar) を数える差し替えに
 * して、「テレメトリを読む者だけが動く」ことを固定する。
 */

const counts = vi.hoisted(() => ({ header: 0, tabBar: 0, checklist: 0 }));

/** Monitor の中身をわざと投げさせるスイッチ。境界の外が生き残ることを見るため */
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

/**
 * ヘッダー本体の再描画を数える検出役。
 *
 * 数えるのは AppHeader が必ず描く末端 (Clock) で、AppHeader 自身は本物のまま
 * 動かす。**ヘッダーごと差し替えると検出役の意味が消える** —— ヘッダーが
 * テレメトリを購読し始めても、描かれているのは差し替えた側なので何も起きない。
 * ここが本物であれば、ヘッダーが 1 度でも描き直された回数がそのまま出る
 * (EMG STOP が境界の外に残ることを見る後段のテストも本物の描画で確かめられる)。
 */
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
      // 本物と同じくテレメトリを購読する。こちらは配信ごとに動くのが正しい
      useRobotStates();
      counts.tabBar += 1;
      return null;
    },
  };
});

/**
 * 準備の面 (MatchPrep) は memo で切り離してある (試合状態しか読まない)。差し替えも memo に
 * しておくと、切り離しを壊すのは「親が毎描画 新しい props を渡す」場合だけになる。
 */
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
    // 購読側は届いた回数ぶん更新される (止まっていたら値が凍る)
    expect(counts.tabBar).toBeGreaterThan(tabsBefore);
  });

  it("Monitor の指差喚呼リストをテレメトリで描き直さない", async () => {
    // 準備フェーズの主役。20 行のチェックリストを毎秒 40 回描き直す理由は無い
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

/**
 * タブ 1 枚の描画例外を、そのタブの中に閉じ込める。
 *
 * **境界が 1 枚も無いと、React ツリー全体がアンマウントしてヘッダーの
 * EMG STOP ボタンごと画面から消える。** 操縦者に残るのは白い画面と、
 * 動き続けている機体だけになる。
 */
describe("画面の描画例外", () => {
  // 例外は境界が握るが、React は必ず console へ出す。テスト出力を汚さない
  let consoleError: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    flags.dashboardThrows = true;
    consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
  });

  afterEach(() => consoleError.mockRestore());

  it("落ちるのはタブの中身だけで、緊急停止ボタンは残る", async () => {
    await renderApp("/monitor");

    // **これが本題。** 押せなければ操縦者は機体を止める手段を画面から失う
    expect(screen.getByRole("button", { name: "緊急停止" })).toBeInTheDocument();
    expect(screen.getByText("この画面の描画に失敗しました")).toBeInTheDocument();
    // 止める手段が残っていることを画面に書く。書かないと操縦者は判断できない
    expect(screen.getByText(/EMG STOP は生きています/)).toBeInTheDocument();
    // 接続バナーも境界の外。落とすと「繋がっているのか」も画面から消える
    expect(screen.getAllByRole("alert").length).toBeGreaterThan(1);
  });

  it("落ちた画面でも緊急停止を送れる", async () => {
    await renderApp("/monitor");
    act(() => latestSocket().open());

    await userEvent.click(screen.getByRole("button", { name: "緊急停止" }));

    expect(latestSocket().sent).toHaveLength(1);
  });

  it("緊急停止オーバーレイは境界の外なので出続ける", async () => {
    // 停止中であることが画面のどこを見ても分かる必要がある唯一の状態。
    // 境界を外枠まで広げると、ここが真っ先に消える
    await renderApp("/monitor");
    act(() => latestSocket().open());
    act(() => latestSocket().receive({ type: "e_stop_state", active: true }));

    expect(screen.getByText("ALL MOTION HALTED")).toBeInTheDocument();
  });

  it("別のタブへ切り替えれば境界は解ける", async () => {
    // 1 枚の画面の不具合で全タブが「描画に失敗しました」のまま固まると、
    // 操縦者は退避先を失う
    await renderApp("/monitor");
    expect(screen.getByText("この画面の描画に失敗しました")).toBeInTheDocument();

    flags.dashboardThrows = false;
    // TabBar はこのファイルで差し替えてあるので、数字キーで移る (どちらも同じ経路)
    await userEvent.keyboard("2");

    expect(screen.queryByText("この画面の描画に失敗しました")).toBeNull();
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
