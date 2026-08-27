import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { MotorCheckButton } from "@/components/motorcheck/MotorCheckButton";
import type { MatchPhase } from "@/lib/protocol";
import type { MotorCheckState } from "@/lib/robotReducer";
import { DEFAULT_MATCH_STATE, EMPTY_MOTOR_CHECK, renderWithRobot } from "@/test/robotContext";

function mount(
  over: { phase?: MatchPhase; eStopActive?: boolean; connected?: boolean } = {},
  check: Partial<MotorCheckState> = {},
) {
  const { phase = "setup", eStopActive = false, connected = true } = over;
  return renderWithRobot(<MotorCheckButton robotName="main_hand" />, {
    connected,
    eStopActive,
    matchState: { ...DEFAULT_MATCH_STATE, phase },
    motorChecks: { main_hand: { ...EMPTY_MOTOR_CHECK, ...check } },
  });
}

const START_BUTTON = { name: "main_hand の動作確認を開始" };

describe("MotorCheckButton", () => {
  it("セッティングタイムでは押せる", () => {
    // 動作確認はこのフェーズの主役。以前は step_index=0 && total_steps>0 を
    // 「シーケンス実行中」と誤読して、準備中ずっとボタンが無効になっていた
    expect(mount({ phase: "setup" }) && screen.getByRole("button", START_BUTTON)).toBeEnabled();
  });

  it("試合中はサーバーと同じ理由で塞ぐ", () => {
    mount({ phase: "match" });
    expect(screen.getByRole("button", START_BUTTON)).toBeDisabled();
    expect(screen.getByText("試合中は動作確認を実行できません")).toBeInTheDocument();
  });

  it("緊急停止中と切断中は塞ぐ", () => {
    const { unmount } = mount({ eStopActive: true });
    expect(screen.getByRole("button", START_BUTTON)).toBeDisabled();
    expect(screen.getByText("緊急停止中は不可")).toBeInTheDocument();
    unmount();

    mount({ connected: false });
    expect(screen.getByText("切断中のため不可")).toBeInTheDocument();
  });

  it("実行中は二重起動させない", () => {
    mount({}, { status: "running" });
    expect(screen.getByRole("button", START_BUTTON)).toBeDisabled();
    expect(screen.getByText("動作確認 実行中")).toBeInTheDocument();
  });

  it("確認してから開始する (いきなりモータを動かさない)", async () => {
    const { context } = mount();

    await userEvent.click(screen.getByRole("button", START_BUTTON));
    expect(context.send).not.toHaveBeenCalled();
    expect(screen.getByText(/周囲の安全を確認してから開始してください/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Start" }));
    expect(context.send).toHaveBeenCalledWith({ type: "motor_check_start", robot: "main_hand" });
  });
});
