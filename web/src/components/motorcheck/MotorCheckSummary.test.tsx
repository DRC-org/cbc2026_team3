import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { MotorCheckSummary } from "@/components/motorcheck/MotorCheckSummary";
import type { MotorCheckSnapshot } from "@/lib/protocol";
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
    // 進み具合は同じ区分の `MotorCheckPanel` が出す。数行のあいだに 2 度並べない
    expect(screen.queryByText("4 / 12")).not.toBeInTheDocument();
  });

  it("最後まで進んだら完了と出す", () => {
    mount({ running: false, step_index: 12, total_steps: 12 });
    expect(screen.getByText("完了")).toBeInTheDocument();
  });

  it("途中で降りたら未完了と出す", () => {
    // 「完了」と紛らわしい表示にしないこと。チェックを付ける根拠が変わる
    mount({ running: false, step_index: 5, total_steps: 12, error: "動作確認を中断しました" });

    expect(screen.getByText("未完了")).toBeInTheDocument();
    // 理由の全文は同じ区分の `MotorCheckPanel` が出す (そちらは失敗時に自分から開く)。
    // ここにも置くと、同じ理由が truncate 版と並んで 2 度読まれる
    expect(screen.queryByText("動作確認を中断しました")).not.toBeInTheDocument();
  });
});
