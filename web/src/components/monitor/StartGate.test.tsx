import { act, fireEvent, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { StartGate } from "@/components/monitor/StartGate";
import { RobotProvider } from "@/context/RobotContext";
import { ARM_GUARD_MS, ARM_TIMEOUT_MS } from "@/hooks/useArmedPress";
import type { HealthSnapshot, ManualState, MatchState, RobotState } from "@/lib/protocol";
import { createRobotContext, DEFAULT_MATCH_STATE, renderWithRobot } from "@/test/robotContext";

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

function robot(health: HealthSnapshot, manual?: ManualState): RobotState {
  return {
    ...(manual ? { manual } : {}),
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
          rx_down: false,
          rx_down_episodes: 0,
          may_affect_workpiece: false,
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

const READY_MATCH_STATE: MatchState = {
  ...DEFAULT_MATCH_STATE,
  can_start_match: true,
  checklists: {
    pre_match: checklist([
      { id: "a", label: "電源投入", checked: true },
      { id: "c", label: "初期位置確認", checked: true },
    ]),
  },
};

describe("StartGate の二度押し", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  function mount() {
    const onStart = vi.fn();
    const view = renderWithRobot(<StartGate onStart={onStart} />, {
      states: HEALTHY_STATES,
      matchState: READY_MATCH_STATE,
    });
    return { onStart, view };
  }

  const startButton = () => screen.getByRole("button", { name: /試合を開始する/ });

  it("1 回目では開始せず、ボタン自身が確認を求める", () => {
    const { onStart } = mount();

    // fake timer 下では userEvent の内部待ちが解けないため fireEvent を使う
    fireEvent.click(screen.getByRole("button", { name: "試合を開始する" }));

    expect(onStart).not.toHaveBeenCalled();
    expect(screen.getByText("もう一度押すと開始します")).toBeInTheDocument();
    expect(screen.getByText("赤コート")).toBeInTheDocument();
    expect(screen.getByText(/START\s*を押すまで機体は動きません/)).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "もう一度押して試合を開始する" }),
    ).toBeInTheDocument();
  });

  it("不感時間を過ぎた 2 回目で開始する", () => {
    const { onStart } = mount();

    fireEvent.click(startButton());
    act(() => vi.advanceTimersByTime(ARM_GUARD_MS));
    fireEvent.click(startButton());

    expect(onStart).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "試合を開始する" })).toBeInTheDocument();
  });

  it("ダブルクリック 1 回では開始しない", () => {
    const { onStart } = mount();

    fireEvent.click(startButton());
    fireEvent.click(startButton());

    expect(onStart).not.toHaveBeenCalled();
    expect(screen.getByText("もう一度押すと開始します")).toBeInTheDocument();
  });

  it("放置すると確認が解け、次の 1 回はまた 1 回目になる", () => {
    const { onStart } = mount();

    fireEvent.click(startButton());
    act(() => vi.advanceTimersByTime(ARM_TIMEOUT_MS));
    expect(screen.getByText("試合を開始できます")).toBeInTheDocument();

    fireEvent.click(startButton());
    expect(onStart).not.toHaveBeenCalled();
  });

  it("開始できない状況を挟んだら確認をやり直させる", () => {
    const onStart = vi.fn();
    const view = renderWithRobot(<StartGate onStart={onStart} />, {
      states: HEALTHY_STATES,
      matchState: READY_MATCH_STATE,
    });

    fireEvent.click(startButton());
    act(() => vi.advanceTimersByTime(ARM_GUARD_MS));

    const rerenderWith = (connected: boolean) =>
      view.rerender(
        <RobotProvider
          value={createRobotContext({
            connected,
            states: HEALTHY_STATES,
            matchState: READY_MATCH_STATE,
          })}
        >
          <StartGate onStart={onStart} />
        </RobotProvider>,
      );

    rerenderWith(false);
    rerenderWith(true);

    expect(screen.getByText("試合を開始できます")).toBeInTheDocument();
    fireEvent.click(startButton());
    expect(onStart).not.toHaveBeenCalled();
  });
});

describe("StartGate の手動操縦警告", () => {
  const MANUAL: ManualState = { mode: "manual", axes: [] };
  const SEQUENCE: ManualState = { mode: "sequence", axes: [] };

  it("手動操縦のままなら、機体名付きの警告を出す", () => {
    renderWithRobot(<StartGate onStart={vi.fn()} />, {
      states: { main_hand: robot(OK_HEALTH, MANUAL), sub_hand: robot(OK_HEALTH, SEQUENCE) },
      matchState: READY_MATCH_STATE,
    });

    expect(screen.getByText(/手動操縦中/)).toBeInTheDocument();
    expect(screen.getByText("Main Hand")).toBeInTheDocument();
    expect(screen.getByText(/機体に要確認があります/)).toBeInTheDocument();
  });

  it("両ハンドとも半自動なら何も出さない", () => {
    renderWithRobot(<StartGate onStart={vi.fn()} />, {
      states: { main_hand: robot(OK_HEALTH, SEQUENCE), sub_hand: robot(OK_HEALTH, SEQUENCE) },
      matchState: READY_MATCH_STATE,
    });

    expect(screen.queryByText(/手動操縦中/)).not.toBeInTheDocument();
    expect(screen.getByText(/全ての指差喚呼が完了しています/)).toBeInTheDocument();
  });

  it("手動でも開始そのものは止めない (可否を決めるのはサーバーの can_start_match だけ)", () => {
    renderWithRobot(<StartGate onStart={vi.fn()} />, {
      states: { main_hand: robot(OK_HEALTH, MANUAL), sub_hand: robot(OK_HEALTH, MANUAL) },
      matchState: READY_MATCH_STATE,
    });

    expect(screen.getByText("試合を開始できます")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "試合を開始する" })).toBeEnabled();
  });
});
