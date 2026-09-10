import { fireEvent, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import type { HealthSnapshot, MatchState, RobotState, SafetyState } from "@/lib/protocol";
import { Dashboard } from "@/pages/Dashboard";
import { motorState } from "@/test/motorState";
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
        rx_down: false,
        rx_down_episodes: 0,
        may_affect_workpiece: false,
      },
    ],
    motors: [],
    detail: null,
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
    pre_match: {
      items: [
        { id: "a", label: "電源投入", checked: true },
        { id: "b", label: "非常停止解除", checked: false },
      ],
      completed: false,
    },
  },
};

describe("Dashboard (セッティングタイム)", () => {
  it("残っている指差喚呼の項目名を画面に 1 度しか描かない", () => {
    renderWithRobot(<Dashboard />, {
      matchState: SETUP,
      states: { main_hand: robot(), sub_hand: robot({ robot: "sub_hand" }) },
    });

    expect(screen.getAllByText(/非常停止解除/)).toHaveLength(1);
  });

  it("動作確認の入口をこの画面に 1 つだけ置く", () => {
    renderWithRobot(<Dashboard />, {
      matchState: SETUP,
      states: { main_hand: robot(), sub_hand: robot({ robot: "sub_hand" }) },
    });

    expect(screen.getAllByRole("button", { name: "動作確認を開始" })).toHaveLength(1);
  });

  it("指差喚呼をこの画面で完結させる (操縦者タブへ往復させない)", () => {
    renderWithRobot(<Dashboard />, {
      matchState: SETUP,
      states: { main_hand: robot(), sub_hand: robot({ robot: "sub_hand" }) },
    });

    expect(screen.getByText("試合準備")).toBeInTheDocument();
    expect(screen.getByLabelText("非常停止解除")).toBeEnabled();
  });

  it("機体の判定文言を画面に 1 度しか描かない", () => {
    const hot = motorState({ temp: 90 });
    const hotHealth = health({
      motors: [
        {
          name: "y_axis_r",
          bus: "can_m3508",
          state: "warning",
          last_feedback_at: null,
          feedback_age_ms: 0,
          temperature: 90,
          detail: null,
        },
      ],
    });
    renderWithRobot(<Dashboard />, {
      matchState: { ...SETUP, can_start_match: true },
      states: {
        main_hand: robot({ motors: { y_axis_r: hot }, health: hotHealth }),
        sub_hand: robot({ robot: "sub_hand" }),
      },
    });

    expect(screen.getAllByText("要確認 1 件")).toHaveLength(1);
  });

  it("どのバス・どのモータかは右カラムで確かめられる", () => {
    renderWithRobot(<Dashboard />, {
      matchState: SETUP,
      states: { main_hand: robot(), sub_hand: robot({ robot: "sub_hand" }) },
    });

    expect(screen.getAllByText("can_m3508")).toHaveLength(2);
    expect(screen.getByText(/機体状態/)).toBeInTheDocument();
  });
});

describe("Dashboard (試合中の手動操縦)", () => {
  const MATCH: MatchState = { ...SETUP, phase: "match", can_start_match: true };

  it("手動中のハンドにチップが出る", () => {
    renderWithRobot(<Dashboard />, {
      matchState: MATCH,
      states: {
        main_hand: robot({ manual: { mode: "manual", axes: [] } }),
        sub_hand: robot({ robot: "sub_hand" }),
      },
    });

    expect(screen.getAllByText("手動操縦中")).toHaveLength(1);
  });

  it("半自動のままなら出さない", () => {
    renderWithRobot(<Dashboard />, {
      matchState: MATCH,
      states: {
        main_hand: robot({ manual: { mode: "sequence", axes: [] } }),
        sub_hand: robot({ robot: "sub_hand" }),
      },
    });

    expect(screen.queryByText("手動操縦中")).toBeNull();
  });
});

describe("Dashboard 準備中の機体状態カラム", () => {
  it("下に続きがあるあいだだけ合図を出す", () => {
    renderWithRobot(<Dashboard />, {
      matchState: SETUP,
      states: { main_hand: robot(), sub_hand: robot({ robot: "sub_hand" }) },
    });

    const panel = screen.getByText("機体状態").closest("section");
    const body = panel?.querySelector(".scroll");
    if (!body) throw new Error("スクロール面が見つからない");

    const stub = (size: { clientHeight: number; scrollHeight: number; scrollTop: number }) => {
      for (const [key, value] of Object.entries(size)) {
        Object.defineProperty(body, key, { value, configurable: true });
      }
      fireEvent.scroll(body);
    };

    stub({ clientHeight: 526, scrollHeight: 1913, scrollTop: 0 });
    expect(panel?.querySelector(".bg-linear-to-t")).not.toBeNull();

    stub({ clientHeight: 526, scrollHeight: 1913, scrollTop: 1387 });
    expect(panel?.querySelector(".bg-linear-to-t")).toBeNull();
  });
});

describe("Dashboard の再励磁", () => {
  const MATCH: MatchState = { ...SETUP, phase: "match", can_start_match: true };
  const unenergized: SafetyState = {
    sync_violations: [],
    unenergized_motors: ["sub_lift"],
    unresponsive_motors: [],
    firmware_unconfirmed_motors: [],
    failed_tasks: [],
    reenergizing: false,
    loops_running: true,
    monitors_running: true,
    limit_monitors_running: true,
    refreshers_running: true,
    position_loops: [],
    sync_monitors: [],
    limit_monitors: [],
    target_refreshers: [],
  };

  it("準備中の機体状態カラムから、無励磁のハンドへ宛てて送る", async () => {
    const { context } = renderWithRobot(<Dashboard />, {
      matchState: SETUP,
      states: {
        main_hand: robot(),
        sub_hand: robot({ robot: "sub_hand", safety: unenergized }),
      },
    });

    const buttons = screen.getAllByRole("button", { name: "再励磁" });
    expect(buttons).toHaveLength(1);

    await userEvent.click(buttons[0]);
    expect(context.sendOrReport).toHaveBeenCalledWith(
      { type: "reenergize_motors", robot: "sub_hand" },
      expect.any(String),
    );
  });

  it("試合中の機体カードからも、そのカードのハンドへ宛てて送る", async () => {
    const { context } = renderWithRobot(<Dashboard />, {
      matchState: MATCH,
      states: {
        main_hand: robot(),
        sub_hand: robot({ robot: "sub_hand", safety: unenergized }),
      },
    });

    const buttons = screen.getAllByRole("button", { name: "再励磁" });
    expect(buttons).toHaveLength(1);

    await userEvent.click(buttons[0]);
    expect(context.sendOrReport).toHaveBeenCalledWith(
      { type: "reenergize_motors", robot: "sub_hand" },
      expect.any(String),
    );
  });

  it("main_hand が無励磁なら main_hand へ宛てる", async () => {
    const { context } = renderWithRobot(<Dashboard />, {
      matchState: MATCH,
      states: {
        main_hand: robot({ safety: unenergized }),
        sub_hand: robot({ robot: "sub_hand" }),
      },
    });

    await userEvent.click(screen.getByRole("button", { name: "再励磁" }));
    expect(context.sendOrReport).toHaveBeenCalledWith(
      { type: "reenergize_motors", robot: "main_hand" },
      expect.any(String),
    );
  });
});
