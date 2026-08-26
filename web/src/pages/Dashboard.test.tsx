import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { HealthSnapshot, MatchState, RobotState } from "@/lib/protocol";
import { Dashboard } from "@/pages/Dashboard";
import { DEFAULT_MATCH_STATE, renderWithRobot } from "@/test/robotContext";

function health(over: Partial<HealthSnapshot> = {}): HealthSnapshot {
  return {
    timestamp: 0,
    overall: "ok",
    buses: [
      {
        name: "can_m3508",
        channel: "can0",
        state: "ok",
        last_tx_at: null,
        last_rx_at: null,
        tx_error_count: 0,
        rx_error_count: 0,
        bus_off: false,
      },
    ],
    motors: [],
    ...over,
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
    health: health(),
    ...over,
  };
}

const SETUP: MatchState = {
  ...DEFAULT_MATCH_STATE,
  phase: "setup",
  can_start_match: false,
  checklists: {
    main_hand: {
      items: [
        { id: "a", label: "電源投入", checked: true },
        { id: "b", label: "非常停止解除", checked: false },
      ],
      completed: false,
    },
    sub_hand: {
      items: [{ id: "c", label: "初期位置確認", checked: true }],
      completed: true,
    },
  },
};

/**
 * 準備フェーズの Monitor は問いを 1 つに絞る:
 * 「試合を開始できるか、できないなら何が足りないか」。
 * 同じ答えを右のパネルが繰り返すと、視線が画面を往復するだけで情報は増えない。
 */
describe("Dashboard (セッティングタイム)", () => {
  it("残っている指差喚呼の項目名を画面に 1 度しか描かない", () => {
    renderWithRobot(<Dashboard />, {
      matchState: SETUP,
      states: { main_hand: robot(), sub_hand: robot({ robot: "sub_hand" }) },
    });

    // 以前は StartGate と右カラムの OperatorProgress が同じ項目を 2 度並べていた
    expect(screen.getAllByText(/非常停止解除/)).toHaveLength(1);
  });

  it("機体の判定文言を画面に 1 度しか描かない", () => {
    const hot = { pos: 0, vel: 0, torque: 0, temp: 90 };
    renderWithRobot(<Dashboard />, {
      matchState: { ...SETUP, can_start_match: true },
      states: {
        main_hand: robot({ motors: { y_axis_r: hot } }),
        sub_hand: robot({ robot: "sub_hand" }),
      },
    });

    // 「要確認 1 件」が StartGate と SubsystemStatus の見出しに同時に出ていた
    expect(screen.getAllByText("要確認 1 件")).toHaveLength(1);
  });

  it("どのバス・どのモータかは右カラムで確かめられる", () => {
    renderWithRobot(<Dashboard />, {
      matchState: SETUP,
      states: { main_hand: robot(), sub_hand: robot({ robot: "sub_hand" }) },
    });

    expect(screen.getAllByText("can_m3508")).toHaveLength(2);
    expect(screen.getByText("試合設定")).toBeInTheDocument();
  });
});
