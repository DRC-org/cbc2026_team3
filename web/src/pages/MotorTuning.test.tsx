import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import type { MatchPhase, MotorPid, MotorState, RobotState } from "@/lib/protocol";
import { MotorTuning } from "@/pages/MotorTuning";
import { DEFAULT_MATCH_STATE, renderWithRobot } from "@/test/robotContext";

/** PC 側 PID を持つモータ。`applies_to` は既定で自分だけ (単独軸) */
function tunable(pid: Partial<MotorPid> = {}, state: Partial<MotorState> = {}): MotorState {
  return {
    pos: 1,
    vel: 0,
    torque: 0,
    temp: 42,
    pid: { kp: 2, ki: 0, kd: 0, applies_to: ["lift"], ...pid },
    ...state,
  };
}

/** ドライバ・ファーム側でループを閉じているモータ。PC からゲインを変更できない */
function fixed(state: Partial<MotorState> = {}): MotorState {
  return { pos: 2, vel: 0, torque: 0, temp: 30, pid: null, ...state };
}

function robotState(): RobotState {
  return {
    robot: "main_hand",
    sequence: "main",
    current_step: null,
    step_index: 0,
    total_steps: 3,
    waiting_trigger: false,
    motors: { lift: tunable() },
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
  it("準備フェーズでは 3 値を 1 通で送れる", async () => {
    const { context } = mount("setup");

    await userEvent.click(screen.getByRole("button", { name: SEND_LABEL }));

    // 3 通に分けると混ざった状態が制御周期をまたいで残り、拒否も 3 連発になる
    expect(context.send).toHaveBeenCalledTimes(1);
    expect(context.send).toHaveBeenCalledWith({
      type: "set_param",
      motor: "lift",
      gains: { kp: 2, ki: 0, kd: 0 },
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

/**
 * 表示する値の出どころはサーバーの `motors[].pid` ただ 1 つ。
 *
 * ここを持たず 0 で初期化していた頃、画面は開いた瞬間に Kp/Ki/Kd を 0.00 と表示し、
 * そのまま送ると config の kp=2.0 が 0 で上書きされて位置制御ループが無効になった。
 * 操縦者には config を読む以外に元の値へ戻す術が無い。
 */
describe("MotorTuning の現在値", () => {
  it("サーバーが配っているゲインを初期値として出す", () => {
    renderWithRobot(<MotorTuning />, {
      states: {
        main_hand: {
          ...robotState(),
          motors: { lift: tunable({ kp: 2.5, ki: 0.25, kd: 0.125 }) },
        },
      },
    });

    expect(screen.getByLabelText("Kp")).toHaveValue(2.5);
    expect(screen.getByLabelText("Ki")).toHaveValue(0.25);
    expect(screen.getByLabelText("Kd")).toHaveValue(0.125);
  });

  it("何も触らずに送ると、いま効いている値がそのまま飛ぶ", async () => {
    const { context } = renderWithRobot(<MotorTuning />, {
      states: {
        main_hand: {
          ...robotState(),
          motors: { lift: tunable({ kp: 2.5, ki: 0.25, kd: 0.125 }) },
        },
      },
    });

    await userEvent.click(screen.getByRole("button", { name: SEND_LABEL }));

    // 0 が 1 つでも混ざれば、その項目のゲインは無言で消える
    expect(context.send).toHaveBeenCalledWith({
      type: "set_param",
      motor: "lift",
      gains: { kp: 2.5, ki: 0.25, kd: 0.125 },
    });
  });

  it("編集した項目は編集値、触っていない項目は現在値を送る", async () => {
    const { context } = mount("setup");

    const kp = screen.getByLabelText("Kp");
    await userEvent.clear(kp);
    await userEvent.type(kp, "4");
    await userEvent.click(screen.getByRole("button", { name: SEND_LABEL }));

    expect(context.send).toHaveBeenCalledWith({
      type: "set_param",
      motor: "lift",
      gains: { kp: 4, ki: 0, kd: 0 },
    });
  });

  it("現在値が作業レンジを超えていたらレンジを広げる", () => {
    // config の値は実機で詰めた結果なので、UI の刻み幅の都合で切ってはならない。
    // クランプすると表示された値と機体の実際のゲインが食い違う
    renderWithRobot(<MotorTuning />, {
      states: {
        main_hand: { ...robotState(), motors: { lift: tunable({ kp: 25 }) } },
      },
    });

    expect(screen.getByLabelText("Kp")).toHaveValue(25);
    expect(screen.getByLabelText("Kp スライダー")).toHaveAttribute("max", "25");
  });
});

/**
 * 左右直結ペアはサーバーがグループ全員へ同じ値を入れる。
 * 片側だけ別特性になると押し合って機構が壊れるため、UI から片側だけを狙う手段は無い。
 * それが画面に出ていないと、逆に「片側だけ調整できる」と読める。
 */
describe("MotorTuning の適用先", () => {
  it("ペア軸は送信先が両側であることを書く", () => {
    renderWithRobot(<MotorTuning />, {
      states: {
        main_hand: {
          ...robotState(),
          motors: { y_axis_r: tunable({ applies_to: ["y_axis_r", "y_axis_l"] }) },
        },
      },
    });

    expect(screen.getByText(/y_axis_r \/ y_axis_l に適用されます/)).toBeInTheDocument();
  });

  it("単独軸には適用先を書かない (自明な情報を足さない)", () => {
    mount("setup");
    expect(screen.queryByText(/に適用されます/)).toBeNull();
  });
});

function twoRobots() {
  return renderWithRobot(<MotorTuning />, {
    states: {
      main_hand: {
        ...robotState(),
        motors: { lift: tunable(), grip: fixed() },
      },
      sub_hand: {
        ...robotState(),
        robot: "sub_hand",
        motors: { rotate_l: tunable({ applies_to: ["rotate_l"] }, { pos: 7.5, temp: 31 }) },
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

    expect(context.send).toHaveBeenCalledTimes(1);
    expect(context.send).toHaveBeenCalledWith({
      type: "set_param",
      motor: "rotate_l",
      gains: { kp: 2, ki: 0, kd: 0 },
    });
  });

  it("PC 側 PID を持たないモータは一覧に出さない", () => {
    // 出しても送信は必ず拒否される。並べると調整できる 2 基がその中に埋もれ、
    // どれが対象なのかを画面から判断できない
    twoRobots();

    expect(screen.queryByRole("button", { name: /^grip\s*\d/ })).toBeNull();
    expect(screen.getByRole("button", { name: /^lift\s*\d/ })).toBeInTheDocument();
  });

  it("同名モータが両機に居ても編集値が混ざらない", async () => {
    // 編集値はロボット名込みで持つ。モータ名だけをキーにすると、片方で入れた値が
    // もう片方の画面に出る
    const { context } = renderWithRobot(<MotorTuning />, {
      states: {
        main_hand: { ...robotState(), motors: { lift: tunable({ kp: 2 }) } },
        sub_hand: { ...robotState(), robot: "sub_hand", motors: { lift: tunable({ kp: 3 }) } },
      },
    });

    const kp = screen.getByLabelText("Kp");
    await userEvent.clear(kp);
    await userEvent.type(kp, "9");

    await userEvent.click(screen.getAllByRole("button", { name: /^lift\s*\d/ })[1]);
    expect(screen.getByLabelText("Kp")).toHaveValue(3);

    await userEvent.click(screen.getByRole("button", { name: SEND_LABEL }));
    expect(context.send).toHaveBeenCalledWith({
      type: "set_param",
      motor: "lift",
      gains: { kp: 3, ki: 0, kd: 0 },
    });
  });

  it("モータ未受信のときは調整面を出さず、接続待ちだと伝える", () => {
    renderWithRobot(<MotorTuning />, { states: {} });

    expect(screen.getByText(/モータ情報なし/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /PID を送信/ })).toBeNull();
  });

  it("モータは届いているが調整対象が無いときは、その理由を書く", () => {
    // 「モータ情報なし」と出すと接続を疑わせる。ここで操縦者が取るべき行動は
    // 待つことではなく、この画面では触れないと理解すること
    renderWithRobot(<MotorTuning />, {
      states: { main_hand: { ...robotState(), motors: { grip: fixed() } } },
    });

    expect(screen.getByText(/調整対象のモータがありません/)).toBeInTheDocument();
    expect(screen.queryByText(/モータ情報なし/)).toBeNull();
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
