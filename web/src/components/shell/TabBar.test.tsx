import { screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";

import { TabBar } from "@/components/shell/TabBar";
import type {
  BusHealthState,
  HealthSnapshot,
  RobotState,
  SafetyState,
} from "@/hooks/useRobotSocket";
import { renderWithRobot } from "@/test/robotContext";

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
      },
    ],
    motors: [],
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
  loops_running: true,
  monitors_running: true,
  position_loops: [],
  sync_monitors: [],
};

function mount(states: Record<string, RobotState>) {
  renderWithRobot(
    <MemoryRouter>
      <TabBar />
    </MemoryRouter>,
    { states },
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
});
