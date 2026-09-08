import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { MotorCheckSummary } from "@/components/motorcheck/MotorCheckSummary";
import type { MotorCheckSnapshot } from "@/lib/protocol";
import { MALFORMED } from "@/lib/protocol";
import { EMPTY_MOTOR_CHECK, renderWithRobot } from "@/test/robotContext";

function mount(over: Partial<MotorCheckSnapshot>) {
  renderWithRobot(<MotorCheckSummary />, {
    motorCheck: { ...EMPTY_MOTOR_CHECK, available: true, blocked_reason: null, ...over },
  });
}

describe("MotorCheckSummary", () => {
  it("未実行では未実行と出す", () => {
    mount({ total_steps: 12 });
    expect(screen.getByText("未実行")).toBeInTheDocument();
  });

  it("実行中は実行中と出す", () => {
    mount({ running: true, step_index: 4, total_steps: 12 });

    expect(screen.getByText("実行中")).toBeInTheDocument();
    expect(screen.queryByText("4 / 12")).not.toBeInTheDocument();
  });

  it("最後まで進んだら完了と出す", () => {
    mount({ running: false, step_index: 12, total_steps: 12 });
    expect(screen.getByText("完了")).toBeInTheDocument();
  });

  it("途中で降りたら未完了と出す", () => {
    mount({ running: false, step_index: 5, total_steps: 12, error: "動作確認を中断しました" });

    expect(screen.getByText("未完了")).toBeInTheDocument();
    expect(screen.queryByText("動作確認を中断しました")).not.toBeInTheDocument();
  });

  it("除外があれば完了と一緒に件数を出す", () => {
    mount({
      running: false,
      step_index: 6,
      total_steps: 6,
      excluded_steps: [{ step: "サブハンド 昇降", missing_axes: ["sub_lift"] }],
    });

    expect(screen.getByText("完了")).toBeInTheDocument();
    expect(screen.getByText("1 ステップ除外")).toBeInTheDocument();
  });

  it("ステップ一覧が読めない配信は、完了と出す場面でも判定不能を添える", () => {
    mount({ running: false, step_index: 6, total_steps: 6, steps: MALFORMED });

    expect(screen.getByText("完了")).toBeInTheDocument();
    expect(screen.getByText("ステップ 判定不能")).toBeInTheDocument();
  });

  it("読めている配信では判定不能を出さない", () => {
    mount({ running: false, step_index: 6, total_steps: 6 });

    expect(screen.queryByText("ステップ 判定不能")).not.toBeInTheDocument();
  });
});
