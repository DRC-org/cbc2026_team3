import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { HealthSnapshot, MatchState, RobotState } from "@/lib/protocol";
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

    // 項目名を出すのは Checklist だけ。StartGate は残り件数しか言わない
    // (以前は StartGate と右カラムの OperatorProgress が同じ項目を 2 度並べていた)
    expect(screen.getAllByText(/非常停止解除/)).toHaveLength(1);
  });

  it("動作確認の入口をこの画面に 1 つだけ置く", () => {
    // 機体ごとの入口があると 2 つを同時に起動でき、両機が同時に動きうる。
    // 統合後は両ハンドを 1 本のシーケンスで駆動するので入口も 1 つ
    renderWithRobot(<Dashboard />, {
      matchState: SETUP,
      states: { main_hand: robot(), sub_hand: robot({ robot: "sub_hand" }) },
    });

    expect(screen.getAllByRole("button", { name: "動作確認を開始" })).toHaveLength(1);
  });

  it("指差喚呼をこの画面で完結させる (操縦者タブへ往復させない)", () => {
    // 操縦者 2 名は同じ場所に立つので、確認は Monitor 1 画面へ集約した
    renderWithRobot(<Dashboard />, {
      matchState: SETUP,
      states: { main_hand: robot(), sub_hand: robot({ robot: "sub_hand" }) },
    });

    expect(screen.getByText("試合準備")).toBeInTheDocument();
    // チェックは操作できる形で出ていること (表示だけでは点検を進められない)
    expect(screen.getByLabelText("非常停止解除")).toBeEnabled();
  });

  it("機体の判定文言を画面に 1 度しか描かない", () => {
    // 過熱の判定はサーバーが持つ (config の temp_warning_c)。UI へは warning で届く
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

    // 「要確認 1 件」が StartGate と SubsystemStatus の見出しに同時に出ていた
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

/**
 * 試合中の Monitor。手動操縦は「機体は動いているのにシーケンスは進まない」
 * 状態を作るので、その理由が Monitor から読めなければならない。
 */
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
