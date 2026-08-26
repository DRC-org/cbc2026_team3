import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import type { MatchPhase, RobotState } from "@/lib/protocol";
import { MotorTuning } from "@/pages/MotorTuning";
import { DEFAULT_MATCH_STATE, renderWithRobot } from "@/test/robotContext";

function robotState(): RobotState {
  return {
    robot: "main_hand",
    sequence: "main",
    current_step: null,
    step_index: 0,
    total_steps: 3,
    waiting_trigger: false,
    motors: { lift: { pos: 1, vel: 0, torque: 0, temp: 42 } },
  };
}

function mount(phase: MatchPhase = "setup", connected = true) {
  return renderWithRobot(<MotorTuning />, {
    states: { main_hand: robotState() },
    connected,
    matchState: { ...DEFAULT_MATCH_STATE, phase },
  });
}

const SEND_LABEL = "lift の PID を送信";

/**
 * PID の差し替えは試合中サーバーが拒否する (`lib/commands.py` の set_param)。
 * 走行中の位置制御ループの特性をその場で変えることになり、同期グループ全体へ
 * 適用されるため、直結した左右軸が負荷下で同時に別特性になる。
 *
 * 拒否トーストが出てから気付くのでは遅い。押す前に分かる必要がある。
 */
describe("MotorTuning の送信ゲート", () => {
  it("準備フェーズでは 3 値をまとめて送れる", async () => {
    const { context } = mount("setup");

    await userEvent.click(screen.getByRole("button", { name: SEND_LABEL }));

    expect(context.send).toHaveBeenCalledTimes(3);
    expect(context.send).toHaveBeenCalledWith({
      type: "set_param",
      motor: "lift",
      key: "kp",
      value: 0,
    });
  });

  it("試合中は送信ボタンを無効にし、理由を書く", async () => {
    const { context } = mount("match");

    const button = screen.getByRole("button", { name: SEND_LABEL });
    expect(button).toBeDisabled();
    expect(screen.getByText("試合中はパラメータを変更できません")).toBeInTheDocument();

    await userEvent.click(button);
    expect(context.send).not.toHaveBeenCalled();
  });

  it("試合終了後は再び送れる (サーバーも finished では通す)", () => {
    mount("finished");
    expect(screen.getByRole("button", { name: SEND_LABEL })).toBeEnabled();
  });

  it("切断中は届かないので送らせない", () => {
    mount("setup", false);

    expect(screen.getByRole("button", { name: SEND_LABEL })).toBeDisabled();
    expect(screen.getByText("切断中のため送信できません")).toBeInTheDocument();
  });
});
