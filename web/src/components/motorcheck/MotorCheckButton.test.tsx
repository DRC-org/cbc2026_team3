import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { MotorCheckButton } from "@/components/motorcheck/MotorCheckButton";
import type { MotorCheckSnapshot } from "@/lib/protocol";
import { EMPTY_MOTOR_CHECK, renderWithRobot } from "@/test/robotContext";

function mount(check: Partial<MotorCheckSnapshot> = {}, connected = true) {
  return renderWithRobot(<MotorCheckButton />, {
    connected,
    motorCheck: { ...EMPTY_MOTOR_CHECK, available: true, blocked_reason: null, ...check },
  });
}

const START_BUTTON = { name: "動作確認を開始" };

describe("MotorCheckButton", () => {
  it("サーバーが許すなら押せる", () => {
    mount();
    expect(screen.getByRole("button", START_BUTTON)).toBeEnabled();
  });

  it("塞ぐ理由はサーバーの blocked_reason をそのまま出す", () => {
    mount({ blocked_reason: "'sub_hand' が手動操縦モードのため動作確認を実行できません" });

    expect(screen.getByRole("button", START_BUTTON)).toBeDisabled();
    expect(
      screen.getByText("'sub_hand' が手動操縦モードのため動作確認を実行できません"),
    ).toBeInTheDocument();
  });

  it("切断中は画面側の理由で塞ぐ (サーバーへ届かないので理由が返らない)", () => {
    mount({}, false);

    expect(screen.getByRole("button", START_BUTTON)).toBeDisabled();
    expect(screen.getByText("切断中のため不可")).toBeInTheDocument();
  });

  it("実行中はその旨を出す", () => {
    mount({ running: true, blocked_reason: "既に動作確認を実行中です" });

    expect(screen.getByRole("button", START_BUTTON)).toBeDisabled();
    expect(screen.getByText("確認実行中...")).toBeInTheDocument();
  });

  it("確認してから開始する (いきなり両機を動かさない)", async () => {
    const { context } = mount();

    await userEvent.click(screen.getByRole("button", START_BUTTON));
    expect(context.sendOrReport).not.toHaveBeenCalled();
    expect(screen.getByText(/両機の可動範囲に人・物がないこと/)).toBeInTheDocument();
    // 零点合わせのボタンは無いので、確認の文面が「中で零点も確定する」ことを伝える唯一の場所
    expect(screen.getByText(/先にリミットスイッチで零点を確定し/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "開始" }));
    expect(context.sendOrReport).toHaveBeenCalledWith(
      { type: "motor_check_start" },
      "動作確認の開始",
    );
  });
});
