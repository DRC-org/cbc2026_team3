import { screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ActionPanel } from "@/components/operator/ActionPanel";
import type { RobotState, SequenceStepInfo } from "@/lib/protocol";
import { renderWithRobot } from "@/test/robotContext";

function step(index: number, label: string, requireTrigger = false): SequenceStepInfo {
  return { index, label, require_trigger: requireTrigger };
}

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
    running: false,
    steps: STEPS,
    motors: {},
    ...overrides,
  } as RobotState;
}

function mount(state: RobotState, extra: Partial<Parameters<typeof ActionPanel>[0]> = {}) {
  const props: Parameters<typeof ActionPanel>[0] = {
    state,
    inMatch: true,
    blockedLabel: "準備中",
    blockedReason: null,
    onStart: vi.fn(),
    onStop: vi.fn(),
    onTrigger: vi.fn(),
    ...extra,
  };
  const view = renderWithRobot(<ActionPanel {...props} />);
  const panel = view.container.querySelector("section");
  if (!panel) throw new Error("ActionPanel のパネルが見つからない");
  return { ...props, panel: panel as HTMLElement };
}

describe("ActionPanel", () => {
  describe("状態表示と主操作の食い違いを起こさない", () => {
    it("未開始なら『待機中』と START を出す (RUNNING を出さない)", () => {
      mount(makeState());

      expect(screen.getByText(/待機中/)).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "シーケンスを先頭から開始" })).toBeEnabled();
      expect(screen.queryByText("RUNNING")).not.toBeInTheDocument();
    });

    it("実行中は RUNNING を出し STOP を押せる", () => {
      mount(makeState({ step_index: 2, running: true }));

      expect(screen.getByText("実行中")).toBeInTheDocument();
      expect(screen.getByText("RUNNING")).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "シーケンスを通常停止" })).toBeEnabled();
    });

    it("許可待ちは NEXT を出す", () => {
      mount(makeState({ step_index: 1, running: true, waiting_trigger: true }));

      expect(screen.getByText(/許可待ち/)).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "次のステップへ進む" })).toBeEnabled();
    });

    it("止めるものが無いとき STOP は押せない", () => {
      mount(makeState());
      expect(screen.getByRole("button", { name: "シーケンスを通常停止" })).toBeDisabled();
    });

    it("STOP で止めた後は RUNNING を出さず、STOP も押せない", () => {
      mount(makeState({ step_index: 3, running: false }));

      expect(screen.queryByText("RUNNING")).not.toBeInTheDocument();
      expect(screen.getByRole("button", { name: "シーケンスを通常停止" })).toBeDisabled();
    });
  });

  describe("中断位置から押す START", () => {
    it("先頭から走り直すことはボタンが言い、状態表示は停止中に留める", () => {
      mount(makeState({ step_index: 3, running: false }));

      expect(screen.getByRole("button", { name: "シーケンスを先頭から再開" })).toBeEnabled();
      expect(screen.getByText("先頭から再開")).toBeInTheDocument();
      expect(screen.getByText("停止中")).toBeInTheDocument();
      expect(screen.queryByText(/待機中/)).not.toBeInTheDocument();
    });

    it("一度も走っていない状態は今までどおり START", () => {
      mount(makeState());

      expect(screen.getByRole("button", { name: "シーケンスを先頭から開始" })).toBeEnabled();
      expect(screen.getByText(/待機中/)).toBeInTheDocument();
    });
  });

  describe("シーケンスの失敗理由", () => {
    it("平常時は 1 ピクセルも出さない", () => {
      mount(makeState());
      expect(screen.queryByText(/ステップ \d+「/)).not.toBeInTheDocument();
    });

    it("どのステップで何が起きたかを出す", () => {
      mount(
        makeState({
          step_index: 3,
          last_error: { step_index: 2, step: "把持姿勢へ", message: "y_axis: 偏差 3.1 > 許容 2.0" },
        }),
      );

      expect(screen.getByText(/ステップ 3「把持姿勢へ」で停止/)).toBeInTheDocument();
      expect(screen.getByText(/偏差 3.1/)).toBeInTheDocument();
    });
  });

  describe("切断中", () => {
    const DISCONNECTED = { blockedReason: "切断中" };

    it("START を押せなくし、理由をボタンに出す", () => {
      mount(makeState(), DISCONNECTED);

      expect(screen.getByRole("button", { name: /操作不可/ })).toBeDisabled();
      expect(screen.getByText("切断中")).toBeInTheDocument();
    });

    it("NEXT を押せなくし、理由をボタンに出す", () => {
      mount(makeState({ step_index: 1, running: true, waiting_trigger: true }), DISCONNECTED);

      expect(screen.queryByRole("button", { name: "次のステップへ進む" })).toBeNull();
      expect(screen.getByRole("button", { name: /操作不可/ })).toBeDisabled();
      expect(screen.getByText("切断中")).toBeInTheDocument();
    });

    it("STOP も押せない (届かない停止で止まったと思わせない)", () => {
      mount(makeState({ step_index: 1, running: true }), DISCONNECTED);

      expect(screen.getByRole("button", { name: "シーケンスを通常停止" })).toBeDisabled();
    });
  });

  describe("NEXT で走る範囲の予告", () => {
    it("走るステップ数と、止まるステップ名を出す", () => {
      mount(makeState({ step_index: 1, running: true, waiting_trigger: true }));

      expect(screen.getByText("2 ステップ先「ハンド閉じる」で停止")).toBeInTheDocument();
    });

    it("許可待ちが現れないまま終端まで走る場合は、止まらないことを言う", () => {
      const straight: SequenceStepInfo[] = [
        step(0, "初期位置へ移動"),
        step(1, "搬送"),
        step(2, "終了姿勢へ"),
      ];
      mount(
        makeState({
          steps: straight,
          total_steps: straight.length,
          step_index: 0,
          running: true,
        }),
      );

      expect(screen.getByText("残り 2 ステップ 停止なし")).toBeInTheDocument();
      expect(screen.queryByText(/で停止/)).not.toBeInTheDocument();
    });

    it("最終ステップではその旨を出す", () => {
      mount(makeState({ step_index: STEPS.length - 1, running: true }));
      expect(screen.getByText("最終ステップ")).toBeInTheDocument();
    });

    it("完走後は終了を伝える", () => {
      mount(makeState({ step_index: STEPS.length }));
      expect(screen.getByText("全ステップ完了")).toBeInTheDocument();
      expect(screen.getByText("終了")).toBeInTheDocument();
    });
  });

  describe("ステップ番号", () => {
    it("現在番号と総数を 1 箇所だけで出す", () => {
      const { panel } = mount(makeState({ step_index: 2, running: true }));

      const text = (panel.textContent ?? "").replaceAll(/\s+/g, "");
      expect(text.match(/3\/6/g) ?? []).toHaveLength(1);
    });
  });

  describe("シーケンス未取得", () => {
    it("チップとボタンが同じことを言う", () => {
      mount(makeState({ total_steps: 0, steps: [], current_step: null }));

      expect(screen.getAllByText("シーケンス未取得").length).toBeGreaterThan(1);
      expect(screen.queryByText("RUNNING")).not.toBeInTheDocument();
      expect(screen.queryByText(/待機中/)).not.toBeInTheDocument();
    });

    it("開始も停止もさせない (押せるボタンが無い)", () => {
      mount(makeState({ total_steps: 0, steps: [], current_step: null }));

      expect(screen.queryByRole("button", { name: "シーケンスを先頭から開始" })).toBeNull();
      expect(screen.getByRole("button", { name: "シーケンスを通常停止" })).toBeDisabled();
      expect(screen.getByRole("button", { name: /操作不可/ })).toBeDisabled();
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
