import { screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ActionPanel } from "@/components/operator/ActionPanel";
import type { RobotState, SequenceStepInfo } from "@/lib/protocol";
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
      // running を推測していた頃は step_index > 0 だけで「実行中」と表示していたため、
      // 止まっている機体に対して RUNNING と STOP 可能を出し続けていた
      mount(makeState({ step_index: 3, running: false }));

      expect(screen.queryByText("RUNNING")).not.toBeInTheDocument();
      expect(screen.getByRole("button", { name: "シーケンスを通常停止" })).toBeDisabled();
    });
  });

  /**
   * `sequence_stop` は `step_index` を保持したまま降りるので、画面は
   * 「4/6・現在ステップ○○」を出したままになる。そこで押す START は**ステップ 0 へ
   * 戻って全工程を走り直す** —— 中断姿勢のまま先頭の動作が走る。同じ「任意ステップ
   * から再開」である `sequence_jump` は確認モーダルを挟むのに、より危険なこちらだけが
   * 素通しで、しかもボタンは「START」、状態は「待機中」と表示していた。
   */
  describe("中断位置から押す START", () => {
    it("先頭から走り直すことをボタンと状態表示の両方で言う", () => {
      mount(makeState({ step_index: 3, running: false }));

      expect(screen.getByRole("button", { name: "シーケンスを先頭から再開" })).toBeEnabled();
      expect(screen.getByText("先頭から再開")).toBeInTheDocument();
      // 「待機中 — START で開始」のままだと、表示と実際の動作が食い違う
      expect(screen.getByText(/先頭から走り直します/)).toBeInTheDocument();
      expect(screen.queryByText(/待機中/)).not.toBeInTheDocument();
    });

    it("一度も走っていない状態は今までどおり START", () => {
      // 試合開始直後の 1 回目に確認を挟むと、最も急ぐ場面で手数が増える
      mount(makeState());

      expect(screen.getByRole("button", { name: "シーケンスを先頭から開始" })).toBeEnabled();
      expect(screen.getByText(/待機中/)).toBeInTheDocument();
    });
  });

  /**
   * 到達タイムアウト・左右ずれ・零点確定失敗はステップ単位の try で握られるので、
   * これを出さないと画面は「待機中」へ戻るだけになる。**3 層保護の第 1 層
   * (`AxisSyncError`) が操縦者から無音になる。**
   */
  describe("シーケンスの失敗理由", () => {
    it("平常時は 1 ピクセルも出さない", () => {
      mount(makeState());
      // 「ここで停止」(NEXT の予告) と紛れないよう、失敗行だけに現れる形で引く
      expect(screen.queryByText(/」で停止/)).not.toBeInTheDocument();
    });

    it("どのステップで何が起きたかを出す", () => {
      mount(
        makeState({
          step_index: 3,
          last_error: { step_index: 2, step: "把持姿勢へ", message: "y_axis: 偏差 3.1 > 許容 2.0" },
        }),
      );

      // 理由だけでは「どこまで動いて止まったか」= 今の機体の姿勢が分からない
      expect(screen.getByText(/ステップ 3「把持姿勢へ」で停止/)).toBeInTheDocument();
      expect(screen.getByText(/偏差 3.1/)).toBeInTheDocument();
    });
  });

  /**
   * `send` は切断中に false を返すだけなので、塞がないと「押したのにボタンは
   * 有効なまま・機体は動かない・トーストも出ない」になる。試合中に最も多く押す
   * NEXT を含む主操作が全部その形だった。
   */
  describe("切断中", () => {
    const DISCONNECTED = { blockedReason: "切断中のため送信できません" };

    it("START を押せなくし、理由をボタンに出す", () => {
      mount(makeState(), DISCONNECTED);

      expect(screen.getByRole("button", { name: /操作不可/ })).toBeDisabled();
      expect(screen.getByText("切断中のため送信できません")).toBeInTheDocument();
    });

    it("NEXT を押せなくし、理由をボタンに出す", () => {
      mount(makeState({ step_index: 1, running: true, waiting_trigger: true }), DISCONNECTED);

      expect(screen.queryByRole("button", { name: "次のステップへ進む" })).toBeNull();
      expect(screen.getByRole("button", { name: /操作不可/ })).toBeDisabled();
      expect(screen.getByText("切断中のため送信できません")).toBeInTheDocument();
    });

    it("STOP も押せない (届かない停止で止まったと思わせない)", () => {
      mount(makeState({ step_index: 1, running: true }), DISCONNECTED);

      expect(screen.getByRole("button", { name: "シーケンスを通常停止" })).toBeDisabled();
    });
  });

  describe("NEXT で走る範囲の予告", () => {
    it("次の許可待ちステップまでを列挙し、そこで打ち切る", () => {
      // step_index=1 (前進) の次は 3(把持姿勢) → 4(ハンド閉じる ✋) で停止
      mount(makeState({ step_index: 1, running: true, waiting_trigger: true }));

      expect(screen.getByText("把持姿勢へ")).toBeInTheDocument();
      expect(screen.getByText("ハンド閉じる")).toBeInTheDocument();
      expect(screen.getByText("ここで停止")).toBeInTheDocument();
      // 停止点より先は予告しない（どこまで動くのか分からなくなる）
      expect(screen.queryByText("搬送")).not.toBeInTheDocument();
    });

    it("最終ステップではその旨を出す", () => {
      mount(makeState({ step_index: STEPS.length - 1, running: true }));
      expect(screen.getByText("これが最終ステップです")).toBeInTheDocument();
    });

    it("完走後は終了を伝える", () => {
      mount(makeState({ step_index: STEPS.length }));
      expect(screen.getByText("全ステップ完了")).toBeInTheDocument();
      expect(screen.getByText("シーケンスは終了しています")).toBeInTheDocument();
    });
  });

  /**
   * シーケンスが 1 件も届いていない状態 (`total_steps === 0`)。サーバー側の
   * 定義ミスや起動途中で実際に起こりうる。ここを暗黙のフォールバックに任せると、
   * 状態表示は「待機中 — START で開始」なのにボタンだけが「RUNNING」を主張し、
   * 操縦者は同じ画面から相反する 2 つの事実を読むことになる。
   */
  describe("シーケンス未取得", () => {
    it("チップとボタンが同じことを言う", () => {
      mount(makeState({ total_steps: 0, steps: [], current_step: null }));

      expect(screen.getAllByText("シーケンス未取得").length).toBeGreaterThan(1);
      expect(screen.queryByText("RUNNING")).not.toBeInTheDocument();
      expect(screen.queryByText(/待機中/)).not.toBeInTheDocument();
    });

    it("開始も停止もさせない (押せるボタンが無い)", () => {
      mount(makeState({ total_steps: 0, steps: [], current_step: null }));

      // START を出すと、ステップの無いシーケンスを開始させることになる
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
