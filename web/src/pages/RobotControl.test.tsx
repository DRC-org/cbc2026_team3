import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeAll, describe, expect, it } from "vitest";

import type { MatchPhase, MatchState, RobotState, SequenceStepInfo } from "@/lib/protocol";
import { RobotControl } from "@/pages/RobotControl";
import { DEFAULT_MATCH_STATE, renderWithRobot } from "@/test/robotContext";

// ステップ一覧は現在地を画面内へ送るために scrollIntoView を呼ぶが、jsdom は
// これを実装していない。表示位置の追従はここでの検証対象ではないので潰す
beforeAll(() => {
  Element.prototype.scrollIntoView = () => {};
});

const STEPS: SequenceStepInfo[] = [
  { index: 0, label: "初期位置へ移動", require_trigger: false },
  { index: 1, label: "把持姿勢へ", require_trigger: true },
  { index: 2, label: "搬送", require_trigger: false },
];

function robotState(over: Partial<RobotState> = {}): RobotState {
  return {
    robot: "sub_hand",
    sequence: "sub_hand",
    current_step: null,
    step_index: 0,
    total_steps: STEPS.length,
    waiting_trigger: false,
    running: false,
    steps: STEPS,
    motors: { rotate_l: { pos: 0, vel: 0, torque: 0, temp: 30 } },
    health: {
      timestamp: 0,
      overall: "ok",
      buses: [
        {
          name: "can_edulite",
          channel: "can1",
          state: "ok",
          last_tx_at: null,
          last_rx_at: null,
          tx_error_count: 0,
          rx_error_count: 0,
          bus_off: false,
        },
      ],
      motors: [],
      detail: null,
    },
    safety: {
      sync_violations: [],
      loops_running: true,
      monitors_running: true,
      refreshers_running: true,
      position_loops: [],
      sync_monitors: [],
      target_refreshers: [],
    },
    ...over,
  };
}

const CHECKLISTS: MatchState["checklists"] = {
  sub_hand: {
    items: [{ id: "s1", label: "サブハンド初期位置", checked: false }],
    completed: false,
  },
};

/**
 * 担当タブは常に `sub_hand` で描く。ページが `robotKey` を素通しせず
 * どこかで機体名を決め打ちすると、メインハンド担当の操作がサブハンドへ
 * (あるいはその逆へ) 飛ぶ。試験ではそれが最も見つけにくい壊れ方になる。
 */
// state に null を渡すと「まだ 1 通も届いていない」状況を再現する
function mount(
  phase: MatchPhase,
  state: RobotState | null = robotState(),
  timer: MatchState["timer"] = null,
) {
  return renderWithRobot(<RobotControl robotKey="sub_hand" label="サブハンド" />, {
    states: state ? { sub_hand: state } : {},
    matchState: { ...DEFAULT_MATCH_STATE, phase, checklists: CHECKLISTS, timer },
  });
}

/** window へ keydown を流す (操縦者は Space を主操作として使う) */
function pressSpace() {
  window.dispatchEvent(new KeyboardEvent("keydown", { key: " ", bubbles: true, cancelable: true }));
}

describe("試合時間タイマーの配置", () => {
  it("試合中は残り時間を出す", () => {
    mount("match", robotState(), { running: true, elapsed_ms: 60_000, duration_ms: 180_000 });

    expect(screen.getByText("2:00")).toBeInTheDocument();
    expect(screen.getByText("残り時間")).toBeInTheDocument();
  });

  it("セッティングタイムには出さない", () => {
    // 準備中の操縦者の仕事は指差喚呼と動作確認だけ。まだ動いていない時計を
    // 置くと、答えるべき問いが 1 つ増える
    mount("setup", robotState(), { running: false, elapsed_ms: 0, duration_ms: 180_000 });

    expect(screen.queryByText("試合時間")).not.toBeInTheDocument();
  });
});

describe("RobotControl の操作先", () => {
  it("主操作はすべて自分の担当機へ宛てて送る", async () => {
    // 2 名の操縦者が別タブで同じ画面を開く。宛先を間違えると、押した本人の
    // 機体ではなくもう一方が動き出す。誤爆に気付くのは動いた後になる
    const { context } = mount("match");

    await userEvent.click(screen.getByRole("button", { name: "シーケンスを先頭から開始" }));
    expect(context.send).toHaveBeenCalledWith({ type: "sequence_start", robot: "sub_hand" });
  });

  it("トリガーと通常停止も同じ宛先に揃える", async () => {
    const { context } = mount("match", robotState({ running: true, waiting_trigger: true }));

    await userEvent.click(screen.getByRole("button", { name: "次のステップへ進む" }));
    expect(context.send).toHaveBeenCalledWith({ type: "trigger", robot: "sub_hand" });

    await userEvent.click(screen.getByRole("button", { name: "シーケンスを通常停止" }));
    expect(context.send).toHaveBeenCalledWith({ type: "sequence_stop", robot: "sub_hand" });
  });

  it("ステップジャンプは確認を経てから宛先付きで送る", async () => {
    // 物理状態を確かめずに途中から再開すると機構をぶつける。確認なしで
    // 飛べる経路ができていないことも併せて守る
    const { context } = mount("match");

    await userEvent.click(screen.getByRole("button", { name: "ステップ 3: 搬送" }));
    expect(context.send).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: "再開" }));
    expect(context.send).toHaveBeenCalledWith({
      type: "sequence_jump",
      robot: "sub_hand",
      step_index: 2,
    });
  });
});

describe("RobotControl のフェーズ別レイアウト", () => {
  it("準備中は指差喚呼と動作確認だけを出し、シーケンス操作は出さない", () => {
    // このフェーズで操縦者がやることは 2 つだけ。押せない主操作ボタンを
    // 並べると「今やること」が埋もれる
    mount("setup");

    expect(screen.getByText("サブハンド セッティング指差喚呼")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "sub_hand の動作確認を開始" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "シーケンスを先頭から開始" })).toBeNull();
    expect(screen.queryByRole("button", { name: "シーケンスを通常停止" })).toBeNull();
  });

  it("試合中は主操作を出し、指差喚呼を残さない", () => {
    mount("match");

    expect(screen.getByRole("button", { name: "シーケンスを先頭から開始" })).toBeEnabled();
    expect(screen.queryByText("サブハンド セッティング指差喚呼")).toBeNull();
  });

  it("試合終了後は操作を塞ぎ、塞いでいる理由を主操作の位置に出す", () => {
    // 押せない理由が書かれていないと、操縦者は WS の切断を疑って
    // 再読み込みを始める (試合直後に最もやってほしくない操作)
    mount("finished");

    expect(screen.getByRole("button", { name: "操作不可: 試合終了" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "シーケンスを通常停止" })).toBeDisabled();
  });

  it("状態未受信でも画面を壊さず、接続待ちだと伝える", () => {
    mount("match", null);

    expect(screen.getByText(/データ未受信/)).toBeInTheDocument();
  });
});

describe("RobotControl の Space ホットキー", () => {
  it("待機中の Space は START に解決する", () => {
    // 操縦者は機体を見ている。主操作を 1 キーに集約し、画面を見ずに押せるようにする
    const { context } = mount("match");

    pressSpace();

    expect(context.send).toHaveBeenCalledTimes(1);
    expect(context.send).toHaveBeenCalledWith({ type: "sequence_start", robot: "sub_hand" });
  });

  it("許可待ちの Space は NEXT に解決する", () => {
    const { context } = mount("match", robotState({ running: true, waiting_trigger: true }));

    pressSpace();

    expect(context.send).toHaveBeenCalledWith({ type: "trigger", robot: "sub_hand" });
  });

  it("実行中の Space は何も送らない (多重トリガーを作らない)", () => {
    const { context } = mount("match", robotState({ running: true }));

    pressSpace();

    expect(context.send).not.toHaveBeenCalled();
  });

  it("準備中の Space は機体を動かさない", () => {
    // 指差喚呼中に机上でキーへ触れても機体が動いてはならない
    const { context } = mount("setup");

    pressSpace();

    expect(context.send).not.toHaveBeenCalled();
  });
});

describe("RobotControl の診断表示", () => {
  it("試合中の平常時は診断を 1 行に畳む", () => {
    // 8 モータ x 4 値を常時出すと「異常があるか」が数字の海に沈む
    mount("match");

    expect(screen.getByRole("button", { expanded: false })).toBeInTheDocument();
    expect(screen.queryByText("can_edulite")).toBeNull();
  });

  it("準備中は同じ部品を開いた状態で出す (配線確認が目的のフェーズ)", () => {
    mount("setup");

    expect(screen.getByRole("button", { expanded: true })).toBeInTheDocument();
    expect(screen.getByText("can_edulite")).toBeInTheDocument();
  });

  it("安全機構の異常は試合中でも自分から開いて主張する", () => {
    // ラッチ中の軸は緊急停止を解除しても動かない。畳んだままでは
    // 「解除したのに動かない」だけが操縦者に残る
    mount(
      "match",
      robotState({
        safety: {
          sync_violations: ["rotate"],
          loops_running: true,
          monitors_running: true,
          refreshers_running: true,
          position_loops: [],
          sync_monitors: [],
          target_refreshers: [],
        },
      }),
    );

    expect(screen.getByRole("button", { expanded: true })).toBeInTheDocument();
    expect(screen.getByText("同期ずれラッチ")).toBeInTheDocument();
  });
});
