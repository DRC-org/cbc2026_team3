import { screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { StartGate } from "@/components/StartGate";
import type { HealthSnapshot, RobotState } from "@/hooks/useRobotSocket";
import { DEFAULT_MATCH_STATE, renderWithRobot } from "@/test/robotContext";

function checklist(items: { id: string; label: string; checked: boolean }[]) {
  return { items, completed: items.every((i) => i.checked) };
}

const OK_HEALTH: HealthSnapshot = { timestamp: 0, overall: "ok", buses: [], motors: [] };

function robot(health: HealthSnapshot): RobotState {
  return {
    robot: "main_hand",
    sequence: "main_hand",
    current_step: null,
    step_index: 0,
    total_steps: 1,
    waiting_trigger: false,
    steps: [],
    motors: {},
    health,
  } as RobotState;
}

const HEALTHY_STATES = { main_hand: robot(OK_HEALTH), sub_hand: robot(OK_HEALTH) };

describe("StartGate", () => {
  it("残っている項目名まで出す (件数だけでは担当者が動けない)", () => {
    renderWithRobot(<StartGate onStart={vi.fn()} />, {
      states: HEALTHY_STATES,
      matchState: {
        ...DEFAULT_MATCH_STATE,
        can_start_match: false,
        checklists: {
          main_hand: checklist([
            { id: "a", label: "電源投入", checked: true },
            { id: "b", label: "非常停止解除", checked: false },
          ]),
          sub_hand: checklist([{ id: "c", label: "初期位置確認", checked: true }]),
        },
      },
    });

    expect(screen.getByText("まだ開始できません")).toBeInTheDocument();
    expect(screen.getByText(/非常停止解除/)).toBeInTheDocument();
    // 完了しているロールは阻害要因に出さない
    expect(screen.queryByText(/初期位置確認/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "試合を開始する" })).toBeDisabled();
  });

  it("全て完了していれば開始できる", () => {
    renderWithRobot(<StartGate onStart={vi.fn()} />, {
      states: HEALTHY_STATES,
      matchState: {
        ...DEFAULT_MATCH_STATE,
        can_start_match: true,
        checklists: {
          main_hand: checklist([{ id: "a", label: "電源投入", checked: true }]),
          sub_hand: checklist([{ id: "c", label: "初期位置確認", checked: true }]),
        },
      },
    });

    expect(screen.getByText("試合を開始できます")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "試合を開始する" })).toBeEnabled();
  });

  it("切断中は開始できない", () => {
    renderWithRobot(<StartGate onStart={vi.fn()} />, {
      connected: false,
      states: HEALTHY_STATES,
      matchState: { ...DEFAULT_MATCH_STATE, can_start_match: true, checklists: {} },
    });

    expect(screen.getByText(/サーバーに接続できていません/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "試合を開始する" })).toBeDisabled();
  });

  it("機体異常は警告として出すが、開始そのものは止めない", () => {
    // サーバーはハードウェア状態で match_start を拒否しない。ここでボタンを殺すと
    // 軽微な警告ひとつで試合を始められなくなる
    const down: HealthSnapshot = {
      timestamp: 0,
      overall: "down",
      buses: [
        {
          name: "can_edulite",
          channel: "can1",
          state: "down",
          last_tx_at: null,
          last_rx_at: null,
          tx_error_count: 0,
          rx_error_count: 0,
          bus_off: true,
        },
      ],
      motors: [],
    };

    renderWithRobot(<StartGate onStart={vi.fn()} />, {
      states: { main_hand: robot(down), sub_hand: robot(OK_HEALTH) },
      matchState: {
        ...DEFAULT_MATCH_STATE,
        can_start_match: true,
        checklists: {
          main_hand: checklist([{ id: "a", label: "電源投入", checked: true }]),
          sub_hand: checklist([{ id: "c", label: "初期位置確認", checked: true }]),
        },
      },
    });

    expect(screen.getByText(/CAN 停止 can_edulite/)).toBeInTheDocument();
    expect(screen.getByText(/機体に要確認があります/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "試合を開始する" })).toBeEnabled();
  });
});
