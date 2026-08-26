import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { MotorCheckPanel } from "@/components/motorcheck/MotorCheckPanel";
import type { MotorCheckRecord } from "@/lib/protocol";
import type { MotorCheckState } from "@/lib/robotReducer";
import { EMPTY_MOTOR_CHECK, renderWithRobot } from "@/test/robotContext";

function record(over: Partial<MotorCheckRecord> = {}): MotorCheckRecord {
  return {
    motor: "gripper",
    bus: "can_generic",
    started_at: 1_700_000_000,
    finished_at: 1_700_000_000,
    result: "passed",
    expected: 5,
    observed: 4.9,
    detail: null,
    ...over,
  };
}

function mount(check: Partial<MotorCheckState> = {}) {
  return renderWithRobot(<MotorCheckPanel robotName="main_hand" isOpen onOpenChange={vi.fn()} />, {
    motorChecks: { main_hand: { ...EMPTY_MOTOR_CHECK, ...check } },
  });
}

describe("MotorCheckPanel", () => {
  it("未実行ではその旨だけを出す", () => {
    mount();
    expect(screen.getByText("動作確認はまだ実行されていません。")).toBeInTheDocument();
  });

  it("合格は期待値と観測値を並べる", () => {
    mount({ status: "completed", records: [record()] });
    expect(screen.getByText("期待 5 → 観測 4.90")).toBeInTheDocument();
  });

  it("期待値を持たない項目に『期待 null』と書かない", () => {
    // 到達位置を判定しない項目 (グリッパの開閉等) では expected が null で届く。
    // TS 側が number と偽っていたため、画面にそのまま "期待 null" と出ていた
    mount({ status: "completed", records: [record({ expected: null })] });

    expect(screen.queryByText(/null/)).not.toBeInTheDocument();
    expect(screen.getByText("観測 4.90")).toBeInTheDocument();
  });

  it("失敗は理由を出す", () => {
    mount({
      status: "completed",
      records: [record({ result: "failed", detail: "フィードバック無応答" })],
    });
    expect(screen.getByText("フィードバック無応答")).toBeInTheDocument();
  });

  it("実行中は中断でき、終了後はリトライできる", async () => {
    const { context, unmount } = mount({
      status: "running",
      current: "gripper",
      progress: { index: 1, total: 3 },
      records: [record({ result: "pending" })],
    });

    await userEvent.click(screen.getByRole("button", { name: "中断" }));
    expect(context.send).toHaveBeenCalledWith({ type: "motor_check_abort", robot: "main_hand" });
    unmount();

    const retry = mount({ status: "completed", records: [record()] });
    await userEvent.click(screen.getByRole("button", { name: "リトライ" }));
    expect(retry.context.send).toHaveBeenCalledWith({
      type: "motor_check_start",
      robot: "main_hand",
    });
  });
});
