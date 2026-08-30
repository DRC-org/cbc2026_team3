import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { MotorCheckButton } from "@/components/motorcheck/MotorCheckButton";
import type { MotorCheckSnapshot } from "@/lib/protocol";
import { EMPTY_MOTOR_CHECK, renderWithRobot } from "@/test/robotContext";

function mount(check: Partial<MotorCheckSnapshot> = {}, connected = true) {
  return renderWithRobot(<MotorCheckButton />, {
    connected,
    // 既定は「起動できる」。塞ぐ条件はサーバーが blocked_reason で配る
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
    // **画面側で導出し直さない。** フェーズや緊急停止から自前で判定すると、
    // サーバーが受け付ける操作を画面が殺す状態が生まれる
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
    expect(context.send).not.toHaveBeenCalled();
    // 動くのは片方ではなく両機。周囲の確認範囲が変わるので文言で明示する
    expect(screen.getByText(/両機の可動範囲に人・物がないこと/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "開始" }));
    expect(context.send).toHaveBeenCalledWith({ type: "motor_check_start" });
  });
});
