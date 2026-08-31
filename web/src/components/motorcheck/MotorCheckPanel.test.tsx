import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { MotorCheckPanel } from "@/components/motorcheck/MotorCheckPanel";
import type { MotorCheckSnapshot, SequenceStepInfo } from "@/lib/protocol";
import { EMPTY_MOTOR_CHECK, renderWithRobot } from "@/test/robotContext";

const STEPS: SequenceStepInfo[] = [
  { index: 0, label: "メインハンド 初期姿勢へ", require_trigger: false },
  { index: 1, label: "メインハンド y 軸 (左右直結ペア)", require_trigger: false },
  { index: 2, label: "サブハンド 電磁弁 6 個 (打音・目視確認)", require_trigger: false },
];

function mount(check: Partial<MotorCheckSnapshot> = {}, connected = true) {
  return renderWithRobot(<MotorCheckPanel isOpen onOpenChange={vi.fn()} />, {
    connected,
    motorCheck: {
      ...EMPTY_MOTOR_CHECK,
      available: true,
      blocked_reason: null,
      steps: STEPS,
      total_steps: STEPS.length,
      ...check,
    },
  });
}

describe("MotorCheckPanel", () => {
  it("何を動かすかをステップ一覧で先に見せる", () => {
    // 押す前に「両機の何がどの順で動くか」が読めないと、周囲の安全確認ができない
    mount();

    expect(screen.getByText("メインハンド 初期姿勢へ")).toBeInTheDocument();
    expect(screen.getByText("サブハンド 電磁弁 6 個 (打音・目視確認)")).toBeInTheDocument();
  });

  it("実行中は今どのステップかを出す", () => {
    mount({ running: true, step_index: 1, current_step: STEPS[1].label });

    expect(screen.getByText("1 / 3")).toBeInTheDocument();
    expect(screen.getAllByText("メインハンド y 軸 (左右直結ペア)").length).toBeGreaterThan(0);
    expect(screen.getByText("実行中")).toBeInTheDocument();
  });

  it("中断・失敗の理由を出す", () => {
    mount({ error: "緊急停止中のため動作確認を中止しました" });

    expect(screen.getByText("動作確認は完了していません")).toBeInTheDocument();
    expect(screen.getByText("緊急停止中のため動作確認を中止しました")).toBeInTheDocument();
  });

  it("合否の列を持たない", () => {
    // 到達判定を持たない軸 (duty / on_off) に「合格」を出すと、動いたかどうかを
    // 機械が見ていないのに見たように読めてしまう
    mount({ running: false, step_index: 3 });

    expect(screen.queryByText("合格")).not.toBeInTheDocument();
    expect(screen.queryByText(/期待/)).not.toBeInTheDocument();
  });

  it("実行中は中断でき、終了後はもう一度実行できる", async () => {
    const { context, unmount } = mount({ running: true, step_index: 1 });

    await userEvent.click(screen.getByRole("button", { name: "中断" }));
    expect(context.send).toHaveBeenCalledWith({ type: "motor_check_abort" });
    unmount();

    const done = mount({ running: false, step_index: 3 });
    await userEvent.click(screen.getByRole("button", { name: "もう一度実行" }));
    expect(done.context.send).toHaveBeenCalledWith({ type: "motor_check_start" });
  });

  /**
   * **既定の mount() がまさにこの状態** (running:false, step_index:0, ステップ表あり)。
   * 実配信のスナップショットもこの形で、以前はフッタが「完了」、全ステップに緑の ✓ が
   * 付いていた。同じ瞬間に MotorCheckSummary は正しく「未実行」を出していた。
   *
   * `config/checklist.yaml` の「アクチュエータ動作確認 完了」は、この誤表示のまま
   * チェックが付く経路になっていた。
   */
  it("未実行を完了と表示しない", () => {
    mount();

    expect(screen.getByText("未実行")).toBeInTheDocument();
    expect(screen.queryByText("完了")).not.toBeInTheDocument();
    // ✓ が付いた行が 1 つも無いこと (走っていないのに通過済みには見せない)
    expect(document.querySelectorAll(".text-success")).toHaveLength(0);
    // まだ 1 度も走っていないので「もう一度」ではない
    expect(screen.getByRole("button", { name: "実行" })).toBeInTheDocument();
  });

  it("完走したときだけ完了を出す", () => {
    mount({ step_index: STEPS.length });

    expect(screen.getByText("完了")).toBeInTheDocument();
    expect(document.querySelectorAll(".text-success")).toHaveLength(STEPS.length);
  });

  it("切断中は実行できず、理由も出す", () => {
    // サーバーへ届かないので拒否も返らない。押せてしまうと「押したのに何も
    // 起きない」だけが残る (起動ボタン側は既に切断を見ていた)
    mount({}, false);

    expect(screen.getByRole("button", { name: /実行/ })).toBeDisabled();
    expect(screen.getByText("切断中のため不可")).toBeInTheDocument();
  });

  it("起動できない構成では実行ボタンを塞ぐ", () => {
    mount({
      available: false,
      steps: [],
      total_steps: 0,
      blocked_reason: "動作確認シーケンスが読み込まれていません",
    });

    expect(screen.getByRole("button", { name: /実行/ })).toBeDisabled();
    expect(screen.getByText(/この構成では動作確認を実行できません/)).toBeInTheDocument();
  });
});
