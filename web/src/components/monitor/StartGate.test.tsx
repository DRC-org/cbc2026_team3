import { act, fireEvent, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { StartGate } from "@/components/monitor/StartGate";
import { RobotProvider } from "@/context/RobotContext";
import { ARM_GUARD_MS, ARM_TIMEOUT_MS } from "@/hooks/useArmedPress";
import type { HealthSnapshot, MatchState, RobotState } from "@/lib/protocol";
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

/**
 * 試合開始の確認は同じボタンの二度押しで取る（確認ダイアログを廃止した）。
 *
 * 守るのは 2 つ。**1 回では始まらない**ことと、**1 回の物理的なダブルクリックが
 * 二度押しとして成立しない**こと。後者が抜けると、ボタンは確認を取っているように
 * 見えて実質 1 クリックで試合が始まる。
 */
describe("StartGate の二度押し", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  // fake timer 下では userEvent の内部待ちが解けないため fireEvent を使う。
  // 連打を「タイマーを進めずに click を 2 発」で表せるので、不感時間の検証にも合う
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

    fireEvent.click(screen.getByRole("button", { name: "試合を開始する" }));

    expect(onStart).not.toHaveBeenCalled();
    expect(screen.getByText("もう一度押すと開始します")).toBeInTheDocument();
    // ダイアログが持っていた情報（コート・機体が動く条件）はここへ移してある
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
    // 開始したら元の表示へ戻す（戻さないと次の 1 回でもう一度送る）
    expect(screen.getByRole("button", { name: "試合を開始する" })).toBeInTheDocument();
  });

  it("ダブルクリック 1 回では開始しない", () => {
    const { onStart } = mount();

    // 時間を進めずに 2 発。物理的なダブルクリックはこの形で届く
    fireEvent.click(startButton());
    fireEvent.click(startButton());

    expect(onStart).not.toHaveBeenCalled();
    // 2 発目を捨てても武装は解かない（解くと連打が永久に 1 回目へ戻り続ける）
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
    // 武装は押した瞬間の状況に紐づく。通信が切れて戻ってきた後の 1 回目を
    // 2 回目として扱うと、操縦者が意図していない時点で試合が始まる
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
