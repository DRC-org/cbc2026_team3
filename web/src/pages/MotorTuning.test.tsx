import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

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

function mount(phase: MatchPhase = "setup", connected = true, eStopActive = false) {
  return renderWithRobot(<MotorTuning />, {
    states: { main_hand: robotState() },
    connected,
    eStopActive,
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

  it("緊急停止中は送らせない (サーバーも set_param を拒否する)", async () => {
    // 同じ原則を実装している動作確認ボタン (MotorCheckButton) は緊急停止を見ている。
    // 片方だけが見ていないと、同じ「緊急停止中は不可」が画面によって食い違う
    const { context } = mount("setup", true, true);

    const button = screen.getByRole("button", { name: SEND_LABEL });
    expect(button).toBeDisabled();
    expect(screen.getByText("緊急停止中はパラメータを変更できません")).toBeInTheDocument();

    await userEvent.click(button);
    expect(context.send).not.toHaveBeenCalled();
  });

  it("切断中は届かないので送らせない", () => {
    mount("setup", false);

    expect(screen.getByRole("button", { name: SEND_LABEL })).toBeDisabled();
    expect(screen.getByText("切断中のため送信できません")).toBeInTheDocument();
  });
});

function twoRobots() {
  return renderWithRobot(<MotorTuning />, {
    states: {
      main_hand: {
        ...robotState(),
        motors: {
          lift: { pos: 1, vel: 0, torque: 0, temp: 42 },
          grip: { pos: 2, vel: 0, torque: 0, temp: 30 },
        },
      },
      sub_hand: {
        ...robotState(),
        robot: "sub_hand",
        motors: { rotate_l: { pos: 7.5, vel: 0, torque: 0, temp: 31 } },
      },
    },
    matchState: { ...DEFAULT_MATCH_STATE, phase: "setup" },
  });
}

/**
 * 調整は 1 基ずつ応答を見ながら詰める作業で、触っている最中に
 * 「今どのモータを見ているのか」が視界から外れてはならない。
 * 全モータを縦に並べていた頃は 1 基を触るだけでスクロールが要り、
 * 隣のモータの数値を自分のものと読み違える余地があった。
 */
describe("MotorTuning の対象選択", () => {
  it("届いているモータのうち先頭を自動で選び、右面をその 1 基に明け渡す", () => {
    twoRobots();

    expect(screen.getByText("Main Hand / lift")).toBeInTheDocument();
    expect(screen.queryByText("Sub Hand / rotate_l")).toBeNull();
  });

  it("左で選び直すと右面がそのモータへ入れ替わる", async () => {
    twoRobots();

    await userEvent.click(screen.getByRole("button", { name: /rotate_l/ }));

    expect(screen.getByText("Sub Hand / rotate_l")).toBeInTheDocument();
    // 見出しは 1 基ぶんだけ。両機ぶんが同時に出ると数値の持ち主が曖昧になる
    expect(screen.queryByText("Main Hand / lift")).toBeNull();
  });

  it("送信は選択中のモータ宛てになる", async () => {
    const { context } = twoRobots();

    await userEvent.click(screen.getByRole("button", { name: /rotate_l/ }));
    await userEvent.click(screen.getByRole("button", { name: "rotate_l の PID を送信" }));

    expect(context.send).toHaveBeenCalledTimes(3);
    for (const [command] of vi.mocked(context.send).mock.calls) {
      expect(command).toMatchObject({ motor: "rotate_l" });
    }
  });

  it("モータ未受信のときは調整面を出さず、接続待ちだと伝える", () => {
    renderWithRobot(<MotorTuning />, { states: {} });

    expect(screen.getByText(/モータ情報なし/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /PID を送信/ })).toBeNull();
  });
});

/**
 * 値を触っただけで飛ぶと、狙いの値へ動かす途中の全ての値が機体へ届く。
 * PID を詰める操作は「行き過ぎてから戻す」形になりがちなので、
 * 送信は明示操作だけに限る。
 */
describe("MotorTuning の編集と送信の分離", () => {
  it("数値を入れただけでは送信しない", async () => {
    const { context } = mount("setup");

    await userEvent.type(screen.getByLabelText("Kp"), "1.5");

    expect(context.send).not.toHaveBeenCalled();
    expect(screen.getByText("スライダー操作だけでは送信されません")).toBeInTheDocument();
  });

  it("試合中でも値の編集自体は塞がない (次に入れる値を用意しておける)", () => {
    mount("match");

    // 塞ぐのは送信だけ。入力欄まで殺すと、試合が終わってから値を作り直すことになる
    expect(screen.getByLabelText("Kp")).toBeEnabled();
    expect(screen.getByLabelText("Kp スライダー")).toBeEnabled();
  });
});
