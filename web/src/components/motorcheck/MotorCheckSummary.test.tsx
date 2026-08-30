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

  it("実行中は進み具合を出す", () => {
    // 指差喚呼で「アクチュエータ動作確認 完了」にチェックする前の判断材料。
    // モーダルを開かずにここで読み切れるようにする
    mount({ running: true, step_index: 4, total_steps: 12 });

    expect(screen.getByText("実行中")).toBeInTheDocument();
    expect(screen.getByText("4 / 12")).toBeInTheDocument();
  });

  it("最後まで進んだら完了と出す", () => {
    mount({ running: false, step_index: 12, total_steps: 12 });
    expect(screen.getByText("完了")).toBeInTheDocument();
  });

  it("途中で降りたら未完了として理由まで出す", () => {
    // 「完了」と紛らわしい表示にしないこと。チェックを付ける根拠が変わる
    mount({ running: false, step_index: 5, total_steps: 12, error: "動作確認を中断しました" });

    expect(screen.getByText("未完了")).toBeInTheDocument();
    expect(screen.getByText("動作確認を中断しました")).toBeInTheDocument();
  });
});
