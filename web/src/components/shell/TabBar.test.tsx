import { screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { TabBar } from "@/components/shell/TabBar";
import { RobotProvider } from "@/context/RobotContext";
import type { BusHealthState, HealthSnapshot, RobotState, SafetyState } from "@/lib/protocol";
import { createRobotContext, renderWithRobot } from "@/test/robotContext";

/**
 * タブ帯が実際に描き直されたかを数える。`Kbd` は TabBarNav の内側にしか無いので、
 * memo が効いていれば increment されない。
 */
const counts = vi.hoisted(() => ({ kbd: 0 }));

vi.mock("@/components/ui/Kbd", () => ({
  Kbd: ({ children }: { children: React.ReactNode }) => {
    counts.kbd += 1;
    return <kbd>{children}</kbd>;
  },
}));

function health(busState: BusHealthState): HealthSnapshot {
  return {
    timestamp: 0,
    overall: busState,
    buses: [
      {
        name: "can_m3508",
        channel: "can0",
        state: busState,
        last_tx_at: null,
        last_rx_at: null,
        tx_error_count: 0,
        rx_error_count: 0,
        bus_off: false,
        rx_down: false,
      },
    ],
    motors: [],
    detail: null,
  };
}

function robot(over: Partial<RobotState> = {}): RobotState {
  return {
    robot: "main_hand",
    sequence: "main_hand",
    current_step: null,
    step_index: 0,
    total_steps: 3,
    waiting_trigger: false,
    running: false,
    steps: [],
    motors: {},
    ...over,
  };
}

const SAFETY: SafetyState = {
  sync_violations: [],
  unenergized_motors: [],
  loops_running: true,
  monitors_running: true,
  refreshers_running: true,
  position_loops: [],
  sync_monitors: [],
  target_refreshers: [],
};

function mount(states: Record<string, RobotState>, connected = true) {
  renderWithRobot(
    <MemoryRouter>
      <TabBar />
    </MemoryRouter>,
    { states, connected },
  );
}

describe("TabBar のタブ LED", () => {
  it("異常が無ければ LED を出さない", () => {
    mount({ main_hand: robot({ health: health("ok") }) });
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
  });

  it("degraded は警告であって異常ではない", () => {
    // 判定が 2 箇所にあった頃、タブは赤 LED、Monitor は黄「要確認」を同時に出していた
    mount({ main_hand: robot({ health: health("degraded") }) });
    expect(screen.getByRole("img", { name: "要確認" })).toBeInTheDocument();
    expect(screen.queryByRole("img", { name: "異常あり" })).not.toBeInTheDocument();
  });

  it("バス停止は異常として出す", () => {
    mount({ main_hand: robot({ health: health("down") }) });
    expect(screen.getByRole("img", { name: "異常あり" })).toBeInTheDocument();
  });

  it("同期ずれラッチは異常として出す (画面を切り替えなくても気付けるように)", () => {
    mount({
      main_hand: robot({
        health: health("ok"),
        safety: { ...SAFETY, sync_violations: ["y_axis"] },
      }),
    });
    expect(screen.getByRole("img", { name: "異常あり" })).toBeInTheDocument();
  });

  it("トリガー待ちは許可待ちとして出す", () => {
    mount({ main_hand: robot({ health: health("ok"), waiting_trigger: true }) });
    expect(screen.getByRole("img", { name: "許可待ち" })).toBeInTheDocument();
  });

  it("ヘルスが読めない配信でも投げず、異常として出す", () => {
    // ここは RouteErrorBoundary の外。投げれば React ツリーごとアンマウントして
    // ヘッダーの緊急停止ボタンまで消える
    const broken = { overall: "ok" } as unknown as RobotState["health"];
    mount({ main_hand: robot({ health: broken }) });

    expect(screen.getByRole("img", { name: "異常あり" })).toBeInTheDocument();
  });

  /**
   * 切断中に手元にあるのは「切れた瞬間の値」でしかない。LED を消したままにすると、
   * 凍った判定を「今の機体は正常」として読ませることになる。
   */
  describe("切断中", () => {
    it("平常だった機体でも灰の LED を出す", () => {
      // 機体タブは 2 枚あり、どちらも「今の状態は分からない」が正しい
      mount({ main_hand: robot({ health: health("ok") }) }, false);
      expect(screen.getAllByRole("img", { name: "通信断" })).toHaveLength(2);
    });

    it("凍った異常判定をそのまま出さない", () => {
      mount({ main_hand: robot({ health: health("down") }) }, false);
      expect(screen.queryByRole("img", { name: "異常あり" })).not.toBeInTheDocument();
    });
  });
});

/**
 * タブ帯は外枠 (AppHeader) の一部で、20Hz × 2 台のテレメトリの中に居る。
 * ここが購読したままだと、`AppShell` を memo で切り離した意味が消えて
 * 毎秒 40 回タブ帯ごと描き直される (`evaluateHealth` の filter も毎回回る)。
 *
 * LED が要るのは異常時と許可待ちだけなので、畳んだ結果が変わらない間は
 * 描き直さない。
 */
describe("TabBar の再描画", () => {
  beforeEach(() => {
    counts.kbd = 0;
  });

  function renderTabs(states: Record<string, RobotState>) {
    const view = renderWithRobot(
      <MemoryRouter>
        <TabBar />
      </MemoryRouter>,
      { states },
    );
    const update = (next: Record<string, RobotState>) =>
      view.rerender(
        <RobotProvider value={createRobotContext({ ...view.context, states: next })}>
          <MemoryRouter>
            <TabBar />
          </MemoryRouter>
        </RobotProvider>,
      );
    return { update };
  }

  it("テレメトリだけが動いてもタブ帯を描き直さない", () => {
    // モータ温度が 0.1℃ 動いただけの配信。LED の中身は 1 つも変わらない
    const { update } = renderTabs({ main_hand: robot({ health: health("ok") }) });
    const before = counts.kbd;
    expect(before).toBeGreaterThan(0);

    for (let i = 0; i < 20; i += 1) {
      update({ main_hand: robot({ health: health("ok"), step_index: i }) });
    }

    expect(counts.kbd).toBe(before);
  });

  it("LED の中身が変われば描き直す (止まっていたら異常を見逃す)", () => {
    const { update } = renderTabs({ main_hand: robot({ health: health("ok") }) });
    const before = counts.kbd;

    update({ main_hand: robot({ health: health("down") }) });

    expect(counts.kbd).toBeGreaterThan(before);
    expect(screen.getByRole("img", { name: "異常あり" })).toBeInTheDocument();
  });
});
