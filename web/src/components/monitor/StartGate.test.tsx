import { screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { StartGate } from "@/components/monitor/StartGate";
import type { HealthSnapshot, RobotState } from "@/lib/protocol";
import { DEFAULT_MATCH_STATE, renderWithRobot } from "@/test/robotContext";

function checklist(items: { id: string; label: string; checked: boolean }[]) {
  return { items, completed: items.every((i) => i.checked) };
}

const OK_HEALTH: HealthSnapshot = {
  timestamp: 0,
  overall: "ok",
  buses: [],
  motors: [],
  detail: null,
};

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
  it("残り件数を出す (項目名は同じ画面の Checklist が出すので繰り返さない)", () => {
    renderWithRobot(<StartGate onStart={vi.fn()} />, {
      states: HEALTHY_STATES,
      matchState: {
        ...DEFAULT_MATCH_STATE,
        can_start_match: false,
        checklists: {
          pre_match: checklist([
            { id: "a", label: "電源投入", checked: true },
            { id: "b", label: "非常停止解除", checked: false },
          ]),
        },
      },
    });

    expect(screen.getByText("まだ開始できません")).toBeInTheDocument();
    expect(screen.getByText("残り 1 件")).toBeInTheDocument();
    // 項目名は Checklist の担当。ここで繰り返すと、同じ 1 行を 2 箇所で読むことになる
    expect(screen.queryByText(/非常停止解除/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "試合を開始する" })).toBeDisabled();
  });

  it("全て完了していれば開始できる", () => {
    renderWithRobot(<StartGate onStart={vi.fn()} />, {
      states: HEALTHY_STATES,
      matchState: {
        ...DEFAULT_MATCH_STATE,
        can_start_match: true,
        checklists: {
          pre_match: checklist([{ id: "a", label: "電源投入", checked: true }]),
        },
      },
    });

    expect(screen.getByText("試合を開始できます")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "試合を開始する" })).toBeEnabled();
  });

  it("サーバーが開始可と言えば、知らないロールのチェックリストが未完でも止めない", () => {
    // 判定を 2 箇所に置くと、サーバーは can_start_match=true なのに画面だけが
    // ボタンを殺す。配信ロールが 1 つ増えただけで試合開始できなくなった実例がある
    renderWithRobot(<StartGate onStart={vi.fn()} />, {
      states: HEALTHY_STATES,
      matchState: {
        ...DEFAULT_MATCH_STATE,
        can_start_match: true,
        checklists: {
          pre_match: checklist([{ id: "a", label: "電源投入", checked: true }]),
          unknown_role: checklist([{ id: "z", label: "知らない項目", checked: false }]),
        },
      },
    });

    expect(screen.getByText("試合を開始できます")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "試合を開始する" })).toBeEnabled();
    expect(screen.queryByText(/知らない項目/)).not.toBeInTheDocument();
  });

  it("開始不可の理由が分からなくても、押せないボタンだけを見せない", () => {
    renderWithRobot(<StartGate onStart={vi.fn()} />, {
      states: HEALTHY_STATES,
      matchState: {
        ...DEFAULT_MATCH_STATE,
        can_start_match: false,
        checklists: { pre_match: checklist([{ id: "a", label: "電源投入", checked: true }]) },
      },
    });

    expect(screen.getByText("まだ開始できません")).toBeInTheDocument();
    expect(screen.getByText(/未完了の項目があります/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "試合を開始する" })).toBeDisabled();
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
      detail: null,
    };

    renderWithRobot(<StartGate onStart={vi.fn()} />, {
      states: { main_hand: robot(down), sub_hand: robot(OK_HEALTH) },
      matchState: {
        ...DEFAULT_MATCH_STATE,
        can_start_match: true,
        checklists: {
          pre_match: checklist([{ id: "a", label: "電源投入", checked: true }]),
        },
      },
    });

    expect(screen.getByText(/CAN 停止 can_edulite/)).toBeInTheDocument();
    expect(screen.getByText(/機体に要確認があります/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "試合を開始する" })).toBeEnabled();
  });
});
