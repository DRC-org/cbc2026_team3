import { screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ActionPanel } from "@/components/ActionPanel";
import type { RobotState, SequenceStepInfo } from "@/hooks/useRobotSocket";
import { renderWithRobot } from "@/test/robotContext";

function step(index: number, label: string, requireTrigger = false): SequenceStepInfo {
  return { index, label, require_trigger: requireTrigger };
}

// 1 → (2) → 3 → 4(✋) → 5 → 6(✋)
const STEPS: SequenceStepInfo[] = [
  step(0, "初期位置へ移動"),
  step(1, "前進", true),
  step(2, "把持姿勢へ"),
  step(3, "ハンド閉じる", true),
  step(4, "搬送"),
  step(5, "リリース", true),
];

function makeState(overrides: Partial<RobotState> = {}): RobotState {
  return {
    robot: "main_hand",
    sequence: "main_hand",
    current_step: "初期位置へ移動",
    step_index: 0,
    total_steps: STEPS.length,
    waiting_trigger: false,
    steps: STEPS,
    motors: {},
    ...overrides,
  } as RobotState;
}

function mount(state: RobotState, extra: Partial<Parameters<typeof ActionPanel>[0]> = {}) {
  const props = {
    state,
    inMatch: true,
    blockedLabel: "準備中",
    showStop: false,
    onStart: vi.fn(),
    onStop: vi.fn(),
    onTrigger: vi.fn(),
    ...extra,
  };
  renderWithRobot(<ActionPanel {...props} />);
  return props;
}

describe("ActionPanel", () => {
  describe("状態表示と主操作の食い違いを起こさない", () => {
    it("未開始なら『待機中』と START を出す (RUNNING を出さない)", () => {
      mount(makeState());

      expect(screen.getByText(/待機中/)).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "シーケンスを先頭から開始" })).toBeEnabled();
      // 以前はここで TriggerButton が RUNNING を出し、同じ画面で表示が食い違っていた
      expect(screen.queryByText("RUNNING")).not.toBeInTheDocument();
    });

    it("実行中は RUNNING を出し STOP を押せる", () => {
      mount(makeState({ step_index: 2 }), { showStop: true });

      expect(screen.getByText("実行中")).toBeInTheDocument();
      expect(screen.getByText("RUNNING")).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "シーケンスを通常停止" })).toBeEnabled();
    });

    it("許可待ちは NEXT を出す", () => {
      mount(makeState({ step_index: 1, waiting_trigger: true }), { showStop: true });

      expect(screen.getByText(/許可待ち/)).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "次のステップへ進む" })).toBeEnabled();
    });

    it("止めるものが無いとき STOP は押せない", () => {
      mount(makeState());
      expect(screen.getByRole("button", { name: "シーケンスを通常停止" })).toBeDisabled();
    });
  });

  describe("NEXT で走る範囲の予告", () => {
    it("次の許可待ちステップまでを列挙し、そこで打ち切る", () => {
      // step_index=1 (前進) の次は 3(把持姿勢) → 4(ハンド閉じる ✋) で停止
      mount(makeState({ step_index: 1, waiting_trigger: true }), { showStop: true });

      expect(screen.getByText("把持姿勢へ")).toBeInTheDocument();
      expect(screen.getByText("ハンド閉じる")).toBeInTheDocument();
      expect(screen.getByText("ここで停止")).toBeInTheDocument();
      // 停止点より先は予告しない（どこまで動くのか分からなくなる）
      expect(screen.queryByText("搬送")).not.toBeInTheDocument();
    });

    it("最終ステップではその旨を出す", () => {
      mount(makeState({ step_index: STEPS.length - 1 }), { showStop: true });
      expect(screen.getByText("これが最終ステップです")).toBeInTheDocument();
    });

    it("完走後は終了を伝える", () => {
      mount(makeState({ step_index: STEPS.length }));
      expect(screen.getByText("全ステップ完了")).toBeInTheDocument();
      expect(screen.getByText("シーケンスは終了しています")).toBeInTheDocument();
    });
  });

  it("試合中以外は主操作を全て塞ぐ", () => {
    mount(makeState(), { inMatch: false });

    expect(screen.getByRole("button", { name: "シーケンスを通常停止" })).toBeDisabled();
    expect(screen.getByRole("button", { name: /操作不可/ })).toBeDisabled();
    expect(
      screen.queryByRole("button", { name: "シーケンスを先頭から開始" }),
    ).not.toBeInTheDocument();
  });
});
