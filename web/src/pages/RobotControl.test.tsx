import { act, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import type {
  ManualAxis,
  ManualState,
  MatchPhase,
  MatchState,
  RobotState,
  SequenceStepInfo,
} from "@/lib/protocol";
import { MALFORMED } from "@/lib/protocol";
import { RobotControl } from "@/pages/RobotControl";
import { motorState } from "@/test/motorState";
import { DEFAULT_MATCH_STATE, EMPTY_HOMING, renderWithRobot } from "@/test/robotContext";

const STEPS: SequenceStepInfo[] = [
  { index: 0, label: "初期位置へ移動", require_trigger: false },
  { index: 1, label: "把持姿勢へ", require_trigger: true },
  { index: 2, label: "搬送", require_trigger: false },
];

function robotState(over: Partial<RobotState> = {}): RobotState {
  return {
    robot: "sub_hand",
    sequence: "sub_hand",
    current_step: null,
    step_index: 0,
    total_steps: STEPS.length,
    waiting_trigger: false,
    running: false,
    steps: STEPS,
    motors: { rotate_l: motorState() },
    health: {
      timestamp: 0,
      overall: "ok",
      buses: [
        {
          name: "can_edulite",
          channel: "can1",
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
    },
    safety: {
      sync_violations: [],
      unenergized_motors: [],
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
    },
    ...over,
  };
}

const CHECKLISTS: MatchState["checklists"] = {
  sub_hand: {
    items: [{ id: "s1", label: "サブハンド初期位置", checked: false }],
    completed: false,
  },
};

function mount(
  phase: MatchPhase,
  state: RobotState | null = robotState(),
  timer: MatchState["timer"] = null,
) {
  return renderWithRobot(<RobotControl robotKey="sub_hand" label="サブハンド" />, {
    states: state ? { sub_hand: state } : {},
    matchState: { ...DEFAULT_MATCH_STATE, phase, checklists: CHECKLISTS, timer },
  });
}

function pressSpace() {
  window.dispatchEvent(new KeyboardEvent("keydown", { key: " ", bubbles: true, cancelable: true }));
}

describe("試合時間タイマーの配置", () => {
  it("試合中は右カラムに残り時間の値を出す", () => {
    mount("match", robotState(), { running: true, elapsed_ms: 60_000, duration_ms: 180_000 });

    const panel = screen.getByText("残り時間").closest("section");
    expect(panel).not.toBeNull();
    expect(within(panel as HTMLElement).getByText("2:00")).toBeInTheDocument();
  });

  it("セッティングタイムには出さない", () => {
    mount("setup", robotState(), { running: false, elapsed_ms: 0, duration_ms: 180_000 });

    expect(screen.queryByText("残り時間")).not.toBeInTheDocument();
  });
});

describe("試合中の右カラム", () => {
  const TIMERS: MatchState["timer"][] = [
    null,
    { running: true, elapsed_ms: 60_000, duration_ms: 180_000 },
  ];

  it("パネルの幅を列に追従させる — クロス軸の指定を持たない", () => {
    for (const timer of TIMERS) {
      const view = mount("match", robotState(), timer);

      const column = screen.getByText("残り時間").closest("section")?.parentElement;
      expect(column?.className).toContain("flex-col");

      const panels = Array.from(column?.children ?? []);
      expect(panels.some((el) => el.textContent?.includes("機体状態"))).toBe(true);

      const crossAxisPinned = panels.flatMap((el) =>
        Array.from(el.classList)
          .filter((name) => name.startsWith("self-"))
          .map((name) => `${el.querySelector("h2")?.textContent}: ${name}`),
      );
      expect(crossAxisPinned).toEqual([]);

      view.unmount();
    }
  });

  it("縦が足りないときは機体状態だけが縮む — 試合時間は潰さない", () => {
    const view = mount(
      "match",
      robotState({
        safety: {
          sync_violations: [],
          unenergized_motors: ["rotate_l", "rotate_r"],
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
        },
      }),
      { running: true, elapsed_ms: 60_000, duration_ms: 180_000 },
    );
    expect(screen.getByRole("button", { expanded: true })).toBeInTheDocument();

    const timerPanel = screen.getByText("残り時間").closest("section");
    const statusPanel = screen.getByText("機体状態").closest("section");

    expect(timerPanel?.classList.contains("shrink-0")).toBe(true);
    expect(statusPanel?.classList.contains("shrink-0")).toBe(false);
    expect(statusPanel?.classList.contains("min-h-0")).toBe(true);
    expect(statusPanel?.classList.contains("flex-1")).toBe(false);

    view.unmount();
  });
});

describe("RobotControl の操作先", () => {
  it("主操作はすべて自分の担当機へ宛てて送る", async () => {
    const { context } = mount("match");

    await userEvent.click(screen.getByRole("button", { name: "シーケンスを先頭から開始" }));
    expect(context.sendOrReport).toHaveBeenCalledWith(
      { type: "sequence_start", robot: "sub_hand" },
      expect.any(String),
    );
  });

  it("トリガーと通常停止も同じ宛先に揃える", async () => {
    const { context } = mount("match", robotState({ running: true, waiting_trigger: true }));

    await userEvent.click(screen.getByRole("button", { name: "次のステップへ進む" }));
    expect(context.sendOrReport).toHaveBeenCalledWith(
      { type: "trigger", robot: "sub_hand" },
      expect.any(String),
    );

    await userEvent.click(screen.getByRole("button", { name: "シーケンスを通常停止" }));
    expect(context.sendOrReport).toHaveBeenCalledWith(
      { type: "sequence_stop", robot: "sub_hand" },
      expect.any(String),
    );
  });

  it("再励磁も自分の担当機へ宛てて送る", async () => {
    const { context } = mount(
      "match",
      robotState({
        safety: {
          sync_violations: [],
          unenergized_motors: ["rotate_l"],
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
        },
      }),
    );

    await userEvent.click(screen.getByRole("button", { name: "再励磁" }));
    expect(context.sendOrReport).toHaveBeenCalledWith(
      { type: "reenergize_motors", robot: "sub_hand" },
      expect.any(String),
    );
  });

  it("ステップジャンプは確認を経てから宛先付きで送る", async () => {
    const { context } = mount("match");

    await userEvent.click(screen.getByRole("button", { name: "ステップ 3: 搬送" }));
    expect(context.sendOrReport).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: "再開" }));
    expect(context.sendOrReport).toHaveBeenCalledWith(
      { type: "sequence_jump", robot: "sub_hand", step_index: 2 },
      expect.any(String),
    );
  });

  it("駆動中は「クリックで再開」と案内しない (押せないものを押せると言わない)", () => {
    mount("match", robotState({ running: true, step_index: 1 }));

    expect(screen.queryByText("クリックで再開")).toBeNull();
    expect(screen.getByText("停止してから選択")).toBeInTheDocument();
  });

  it("駆動中はステップジャンプの確認モーダルを開かない", async () => {
    mount("match", robotState({ running: true, step_index: 1 }));

    await userEvent.click(screen.getByRole("button", { name: "ステップ 3: 搬送" }));

    expect(screen.queryByRole("button", { name: "再開" })).toBeNull();
    expect(document.querySelector(".modal")).toBeNull();
  });

  it("トリガー待ちではステップジャンプできる (機体は止まっている)", async () => {
    const { context } = mount(
      "match",
      robotState({ running: true, waiting_trigger: true, step_index: 1 }),
    );

    await userEvent.click(screen.getByRole("button", { name: "ステップ 3: 搬送" }));
    await userEvent.click(screen.getByRole("button", { name: "再開" }));

    expect(context.sendOrReport).toHaveBeenCalledWith(
      { type: "sequence_jump", robot: "sub_hand", step_index: 2 },
      expect.any(String),
    );
  });
});

describe("RobotControl のフェーズ別レイアウト", () => {
  it("準備中はシーケンス操作を出さない", () => {
    mount("setup");

    expect(screen.queryByRole("button", { name: "シーケンスを先頭から開始" })).toBeNull();
    expect(screen.queryByRole("button", { name: "シーケンスを通常停止" })).toBeNull();
  });

  it("指差喚呼と動作確認はこの画面に出さない (Monitor の設定面へ集約した)", () => {
    mount("setup");

    expect(screen.queryByText(/セッティング指差喚呼/)).toBeNull();
    expect(screen.queryByRole("button", { name: /動作確認を開始/ })).toBeNull();
  });

  it("試合中は主操作を出す", () => {
    mount("match");

    expect(screen.getByRole("button", { name: "シーケンスを先頭から開始" })).toBeEnabled();
  });

  it("試合終了後は操作を塞ぎ、塞いでいる理由を主操作の位置に出す", () => {
    mount("finished");

    expect(screen.getByRole("button", { name: "操作不可: 試合終了" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "シーケンスを通常停止" })).toBeDisabled();
  });

  it("状態未受信でも画面を壊さず、接続待ちだと伝える", () => {
    mount("match", null);

    expect(screen.getByText(/データ未受信/)).toBeInTheDocument();
  });
});

describe("RobotControl の Space ホットキー", () => {
  it("待機中の Space は START に解決する", () => {
    const { context } = mount("match");

    pressSpace();

    expect(context.sendOrReport).toHaveBeenCalledTimes(1);
    expect(context.sendOrReport).toHaveBeenCalledWith(
      { type: "sequence_start", robot: "sub_hand" },
      expect.any(String),
    );
  });

  it("許可待ちの Space は NEXT に解決する", () => {
    const { context } = mount("match", robotState({ running: true, waiting_trigger: true }));

    pressSpace();

    expect(context.sendOrReport).toHaveBeenCalledWith(
      { type: "trigger", robot: "sub_hand" },
      expect.any(String),
    );
  });

  it("実行中の Space は何も送らない (多重トリガーを作らない)", () => {
    const { context } = mount("match", robotState({ running: true }));

    pressSpace();

    expect(context.sendOrReport).not.toHaveBeenCalled();
  });

  it("準備中の Space は機体を動かさない", () => {
    const { context } = mount("setup");

    pressSpace();

    expect(context.sendOrReport).not.toHaveBeenCalled();
  });
});

describe("RobotControl の診断表示", () => {
  it("試合中の平常時は診断を 1 行に畳む", () => {
    mount("match");

    expect(screen.getByRole("button", { expanded: false })).toBeInTheDocument();
    expect(screen.queryByText("can_edulite")).toBeNull();
  });

  it("準備中は同じ部品を開いた状態で出す (配線確認が目的のフェーズ)", () => {
    mount("setup");

    expect(screen.getByRole("button", { expanded: true })).toBeInTheDocument();
    expect(screen.getByText("can_edulite")).toBeInTheDocument();
  });

  it("安全機構の異常は試合中でも自分から開いて主張する", () => {
    mount(
      "match",
      robotState({
        safety: {
          sync_violations: ["rotate"],
          unenergized_motors: [],
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
        },
      }),
    );

    expect(screen.getByRole("button", { expanded: true })).toBeInTheDocument();
    expect(screen.getByText("同期ずれラッチ rotate")).toBeInTheDocument();
    expect(screen.getByText(/解除し直して/)).toBeInTheDocument();
  });
});

const MANUAL: ManualState = {
  mode: "manual",
  axes: [
    {
      name: "rotate",
      unit: "deg",
      command_mode: "position",
      value: 3,
      target: null,
      manual: { min: -5, max: 30, steps: [1, 5] },
      manual_always: false,
      deviation: 0.1,
      sync_tolerance: 1.0,
      positions: [
        { name: "home", value: 0 },
        { name: "pick", value: 20 },
      ],
      motors: ["rotate_r", "rotate_l"],
    },
  ],
};

function mountManual(
  phase: MatchPhase,
  overrides: Partial<Parameters<typeof renderWithRobot>[1]> = {},
  manual: ManualState = MANUAL,
) {
  return renderWithRobot(<RobotControl robotKey="sub_hand" label="サブハンド" />, {
    states: { sub_hand: robotState({ manual }) },
    matchState: { ...DEFAULT_MATCH_STATE, phase, checklists: CHECKLISTS, timer: null },
    ...overrides,
  });
}

describe("手動操縦モード", () => {
  it("配信を受け取るまでは半自動として描く", () => {
    mount("match", robotState({ manual: undefined }));

    expect(screen.getByRole("button", { name: "半自動" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "手動操縦" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
  });

  it("モード帯はどのフェーズでも同じ位置に出る", () => {
    for (const phase of ["setup", "match", "finished"] as MatchPhase[]) {
      const view = mount(phase);
      expect(screen.getByRole("button", { name: "手動操縦" })).toBeInTheDocument();
      view.unmount();
    }
  });

  it("切り替えは自分の担当機へ宛てて送る", async () => {
    const { context } = mount("match");

    await userEvent.click(screen.getByRole("button", { name: "手動操縦" }));

    expect(context.sendOrReport).toHaveBeenCalledWith(
      { type: "set_operation_mode", robot: "sub_hand", mode: "manual" },
      expect.any(String),
    );
  });

  it("試合中は手動パネルがシーケンスの操作面を置き換える", () => {
    mountManual("match");

    expect(screen.getByLabelText("rotate を 1deg 進める")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "シーケンスを先頭から開始" })).toBeNull();
  });

  it("準備中は手動パネルが指差喚呼を置き換える", () => {
    mountManual("setup");

    expect(screen.getByLabelText("rotate を home へ")).toBeInTheDocument();
    expect(screen.queryByText(/セッティング指差喚呼/)).toBeNull();
  });

  it("試合終了後も手動で動かせる", () => {
    mountManual("finished");

    expect(screen.getByLabelText("rotate を 1deg 進める")).toBeEnabled();
  });

  it("手動中は Space が sequence_start にならない", async () => {
    const { context } = mountManual("match");

    pressSpace();

    expect(context.sendOrReport).not.toHaveBeenCalled();
  });

  it("緊急停止中は理由を出して指令を塞ぐ", async () => {
    const { context } = mountManual("match", { eStopActive: true });

    expect(screen.getAllByText("緊急停止中は手動操縦できません").length).toBeGreaterThan(0);
    await userEvent.click(screen.getByLabelText("rotate を home へ"));
    expect(context.send).not.toHaveBeenCalled();
  });

  it("コート未設定なら理由を出して手動指令を塞ぐ", async () => {
    const { context } = mountManual("setup", {
      states: { sub_hand: robotState({ manual: MANUAL, court_required: true }) },
      matchState: { ...DEFAULT_MATCH_STATE, phase: "setup", court: null, timer: null },
    });

    expect(
      screen.getAllByText(
        "コートが未設定のため手動操縦できません (試合準備でコートを選んでください)",
      ).length,
    ).toBeGreaterThan(0);
    await userEvent.click(screen.getByLabelText("rotate を home へ"));
    expect(context.send).not.toHaveBeenCalled();
  });

  it("コート確定が要らない台は未設定でも手動できる", async () => {
    const { context } = mountManual("setup", {
      states: { sub_hand: robotState({ manual: MANUAL, court_required: false }) },
      matchState: { ...DEFAULT_MATCH_STATE, phase: "setup", court: null, timer: null },
    });

    await userEvent.click(screen.getByLabelText("rotate を home へ"));
    expect(context.sendOrReport).toHaveBeenCalledWith(
      { type: "manual_move", robot: "sub_hand", axis: "rotate", position: "home" },
      "プリセット移動",
    );
  });

  it("緊急停止中でもモード切替は送れる", async () => {
    const { context } = mountManual(
      "match",
      { eStopActive: true },
      { mode: "sequence", axes: MANUAL.axes },
    );

    await userEvent.click(screen.getByRole("button", { name: "手動操縦" }));

    expect(context.sendOrReport).toHaveBeenCalledWith(
      { type: "set_operation_mode", robot: "sub_hand", mode: "manual" },
      expect.any(String),
    );
  });

  it("手動中は機体状態を畳まない", () => {
    mountManual("match");

    expect(screen.getByRole("button", { expanded: true })).toBeInTheDocument();
  });
});

const ALWAYS_MANUAL_AXIS: ManualAxis = {
  name: "conveyor",
  unit: "duty",
  command_mode: "duty",
  value: null,
  target: null,
  manual: null,
  manual_always: true,
  deviation: null,
  sync_tolerance: null,
  positions: [
    { name: "stop", value: 0 },
    { name: "run", value: 0.3 },
  ],
  motors: ["conveyor"],
};

const SEQUENCE_WITH_ALWAYS: ManualState = {
  mode: "sequence",
  axes: [...MANUAL.axes, ALWAYS_MANUAL_AXIS],
};

describe("常時操作パネル", () => {
  it("試合中の半自動に出す", () => {
    mount("match", robotState({ manual: SEQUENCE_WITH_ALWAYS }));

    expect(screen.getByText("常時操作")).toBeInTheDocument();
    expect(screen.getByLabelText("conveyor を stop へ")).toBeEnabled();
    expect(screen.queryByLabelText("rotate を home へ")).toBeNull();
  });

  it("準備中には出さない", () => {
    mount("setup", robotState({ manual: SEQUENCE_WITH_ALWAYS }));

    expect(screen.queryByText("常時操作")).toBeNull();
    expect(screen.queryByLabelText("conveyor を stop へ")).toBeNull();
  });

  it("手動モード中には出さない (手動パネルが同じ軸を出す)", () => {
    mountManual("match", {}, { ...SEQUENCE_WITH_ALWAYS, mode: "manual" });

    expect(screen.queryByText("常時操作")).toBeNull();
    expect(screen.getAllByLabelText("conveyor を stop へ")).toHaveLength(1);
  });
});

describe("中断位置から押す START", () => {
  const stopped = () => robotState({ step_index: 1, running: false });

  it("確認を経てから sequence_start を送る", async () => {
    const { context } = mount("match", stopped());

    await userEvent.click(screen.getByRole("button", { name: "シーケンスを先頭から再開" }));
    expect(context.sendOrReport).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: "先頭から実行" }));
    expect(context.sendOrReport).toHaveBeenCalledWith(
      { type: "sequence_start", robot: "sub_hand" },
      expect.any(String),
    );
  });

  it("何が起きるかと、途中から再開する手段を書く", async () => {
    mount("match", stopped());

    await userEvent.click(screen.getByRole("button", { name: "シーケンスを先頭から再開" }));

    expect(screen.getByText(/全工程を走り直します/)).toBeInTheDocument();
    expect(screen.getByText(/ステップ一覧から再開するステップを選んで/)).toBeInTheDocument();
    expect(screen.getByText(/物理状態が安全であることを必ず確認/)).toBeInTheDocument();
  });

  it("キャンセルすれば 1 通も送らない", async () => {
    const { context } = mount("match", stopped());

    await userEvent.click(screen.getByRole("button", { name: "シーケンスを先頭から再開" }));
    await userEvent.click(screen.getByRole("button", { name: "キャンセル" }));

    expect(context.sendOrReport).not.toHaveBeenCalled();
  });

  it("Space 1 打では走り出さない (キーの方がボタンより危ない)", () => {
    const { context } = mount("match", stopped());

    act(() => pressSpace());

    expect(context.sendOrReport).not.toHaveBeenCalled();
    expect(screen.getByText(/全工程を走り直します/)).toBeInTheDocument();
  });

  it("一度も走っていない状態は確認を挟まない (試合開始直後の 1 回目)", async () => {
    const { context } = mount("match", robotState({ step_index: 0, running: false }));

    await userEvent.click(screen.getByRole("button", { name: "シーケンスを先頭から開始" }));

    expect(context.sendOrReport).toHaveBeenCalledWith(
      { type: "sequence_start", robot: "sub_hand" },
      expect.any(String),
    );
  });
});

describe("RobotControl の切断中", () => {
  it("主操作を押せなくし、理由を出す", () => {
    const view = renderWithRobot(<RobotControl robotKey="sub_hand" label="サブハンド" />, {
      connected: false,
      states: { sub_hand: robotState({ running: true, waiting_trigger: true }) },
      matchState: { ...DEFAULT_MATCH_STATE, phase: "match", checklists: CHECKLISTS },
    });

    expect(screen.queryByRole("button", { name: "次のステップへ進む" })).toBeNull();
    expect(view.container.querySelectorAll("button[disabled]").length).toBeGreaterThan(0);
    expect(screen.getAllByText("切断中のため送信できません").length).toBeGreaterThan(0);
  });

  it("ステップジャンプも押せなくする", () => {
    renderWithRobot(<RobotControl robotKey="sub_hand" label="サブハンド" />, {
      connected: false,
      states: { sub_hand: robotState() },
      matchState: { ...DEFAULT_MATCH_STATE, phase: "match", checklists: CHECKLISTS },
    });

    expect(screen.getByRole("button", { name: "ステップ 3: 搬送" })).toBeDisabled();
  });
});

function stepPanel(): HTMLElement {
  const panel = screen.getByText("ステップ").closest("section");
  if (!panel) throw new Error("ステップ一覧のパネルが見つからない");
  return panel as HTMLElement;
}

describe("ステップ一覧の見出し", () => {
  it("操作できるときは案内を出さない", () => {
    mount("match");

    expect(within(stepPanel()).queryByText("クリックで再開")).toBeNull();
  });

  it("試合中でなければ、塞がれている理由を出す", () => {
    mount("finished");

    expect(within(stepPanel()).getByText("試合中のみ操作可")).toBeInTheDocument();
  });

  it("切断中も、塞がれている理由を出す", () => {
    renderWithRobot(<RobotControl robotKey="sub_hand" label="サブハンド" />, {
      connected: false,
      states: { sub_hand: robotState() },
      matchState: { ...DEFAULT_MATCH_STATE, phase: "match", checklists: CHECKLISTS },
    });

    expect(within(stepPanel()).getByText("切断中のため送信できません")).toBeInTheDocument();
  });
});

describe("シーケンス名の置き場所", () => {
  it("準備中はモード帯にシーケンス名を出す", () => {
    mount("setup");

    expect(screen.getByText("sub_hand")).toBeInTheDocument();
    expect(screen.queryByText("シーケンス")).toBeNull();
  });

  it("半自動では総ステップ数を出さない (準備中は一覧が、試合中は ActionPanel が出す)", () => {
    mount("setup");
    expect(screen.queryByText(/全 3 ステップ/)).toBeNull();

    mount("match");
    expect(screen.queryByText(/全 3 ステップ/)).toBeNull();
  });

  it("手動の準備中だけ帯が総ステップ数を引き受ける (一覧が画面から消えるため)", () => {
    mountManual("setup");

    expect(screen.getByText(/全 3 ステップ/)).toBeInTheDocument();
  });
});

describe("準備中のステップ一覧", () => {
  it("半自動の準備中にも一覧を出す", () => {
    mount("setup");

    expect(screen.getByRole("button", { name: "ステップ 3: 搬送" })).toBeInTheDocument();
  });

  it("押せなくし、その理由を出す", () => {
    mount("setup");

    expect(screen.getByRole("button", { name: "ステップ 3: 搬送" })).toBeDisabled();
    expect(within(stepPanel()).getByText("試合中のみ操作可")).toBeInTheDocument();
  });

  it("手動中は出さない (同じ列を手元の操作面へ明け渡す)", () => {
    mountManual("setup");

    expect(screen.queryByRole("button", { name: "ステップ 3: 搬送" })).toBeNull();
  });
});

describe("吸着パッドの面", () => {
  const SUCTION: RobotState["suction"] = {
    pads: [
      { axis: "valve_1", label: "1", enabled: true },
      { axis: "valve_2", label: "2", enabled: true },
    ],
  };

  const VALVE_AXES: ManualAxis[] = ["valve_1", "valve_2"].map((name) => ({
    name,
    unit: "on_off",
    command_mode: "on_off",
    value: null,
    target: 0,
    manual: null,
    manual_always: true,
    deviation: null,
    sync_tolerance: null,
    positions: [
      { name: "closed", value: 0 },
      { name: "open", value: 1 },
    ],
    motors: [name],
  }));

  // 半自動は「次の吸着で使う弁の宣言」、手動は「今すぐ開閉」で面の役割が変わる
  const GROUP_NAME: Record<ManualState["mode"], string> = {
    sequence: "吸着に使うパッド",
    manual: "吸着パッドの開閉",
  };

  it.each<[MatchPhase, ManualState["mode"]]>([
    ["setup", "sequence"],
    ["setup", "manual"],
    ["match", "sequence"],
    ["match", "manual"],
  ])("%s の %s モードでも出す", (phase, mode) => {
    mount(phase, robotState({ suction: SUCTION, manual: { mode, axes: VALVE_AXES } }));

    expect(screen.getByRole("group", { name: GROUP_NAME[mode] })).toBeInTheDocument();
  });

  it("吸着パッドを持たないロボットには出さない", () => {
    mount("match", robotState({ suction: null }));

    expect(screen.queryByRole("group", { name: "吸着に使うパッド" })).toBeNull();
  });

  it("選択は自分の担当機へ宛てて送る", async () => {
    const { context } = mount("match", robotState({ suction: SUCTION }));

    await userEvent.click(screen.getByRole("button", { name: "パッド 2 を使わない" }));

    expect(context.sendOrReport).toHaveBeenCalledWith(
      { type: "suction_pads_set", robot: "sub_hand", pads: ["valve_1"] },
      expect.any(String),
    );
  });

  it("手動でもパッドの弁を手動操縦へ二度描きしない", () => {
    mount("match", robotState({ suction: SUCTION, manual: { mode: "manual", axes: VALVE_AXES } }));

    expect(screen.getByRole("group", { name: "吸着パッドの開閉" })).toBeInTheDocument();
    expect(screen.queryByRole("group", { name: "開閉トグル" })).toBeNull();
    expect(screen.queryByLabelText("valve_1 を ON にする")).toBeNull();
  });

  it("読み取れない配信では弁の操作口を手動操縦に残す", () => {
    mount(
      "match",
      robotState({ suction: MALFORMED, manual: { mode: "manual", axes: VALVE_AXES } }),
    );

    expect(screen.getByText("吸着パッドの状態を読み取れませんでした")).toBeInTheDocument();
    expect(screen.getByLabelText("valve_1 を ON にする")).toBeInTheDocument();
  });
});

describe("零点合わせ", () => {
  const HOMING = {
    ...EMPTY_HOMING,
    available: true,
    blocked_reason: null,
    targets: { main_hand: ["y_axis"], sub_hand: ["sub_y_axis"] },
  };
  const SUB_BUTTON = { name: "サブハンドの零点合わせを開始" };

  it("準備中の半自動に、自分の担当機のボタンだけ出す", () => {
    renderWithRobot(<RobotControl robotKey="sub_hand" label="サブハンド" />, {
      states: { sub_hand: robotState() },
      matchState: { ...DEFAULT_MATCH_STATE, phase: "setup", checklists: CHECKLISTS },
      homing: HOMING,
    });

    expect(screen.getByRole("button", SUB_BUTTON)).toBeEnabled();
    expect(screen.queryByRole("button", { name: "メインハンドの零点合わせを開始" })).toBeNull();
  });

  it("手動中はボタンを出さず、サーバーが配る拒否理由だけを出す", () => {
    const reason = "'sub_hand' が手動操縦モードのため零点合わせを実行できません";
    mountManual("setup", { homing: { ...HOMING, blocked_reason: reason } });

    expect(screen.queryByRole("button", SUB_BUTTON)).toBeNull();
    expect(screen.getByText(reason)).toBeInTheDocument();
  });

  it("試合中は出さない (サーバーがフェーズで拒む)", () => {
    renderWithRobot(<RobotControl robotKey="sub_hand" label="サブハンド" />, {
      states: { sub_hand: robotState() },
      matchState: { ...DEFAULT_MATCH_STATE, phase: "match", checklists: CHECKLISTS },
      homing: HOMING,
    });

    expect(screen.queryByText("零点合わせ")).toBeNull();
  });
});
