import { act, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import type {
  ManualState,
  MatchPhase,
  MatchState,
  RobotState,
  SequenceStepInfo,
} from "@/lib/protocol";
import { RobotControl } from "@/pages/RobotControl";
import { motorState } from "@/test/motorState";
import { DEFAULT_MATCH_STATE, renderWithRobot } from "@/test/robotContext";

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
    motors: { rotate_l: motorState() },
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
          rx_down: false,
          rx_down_episodes: 0,
          may_affect_workpiece: false,
        },
      ],
      motors: [],
      detail: null,
    },
    safety: {
      sync_violations: [],
      unenergized_motors: [],
      firmware_unconfirmed_motors: [],
      failed_tasks: [],
      reenergizing: false,
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
  it("試合中は右カラムに残り時間の値を出す", () => {
    mount("match", robotState(), { running: true, elapsed_ms: 60_000, duration_ms: 180_000 });

    // 進行中は caption を出さない (数字が残り時間であることは legend から読める)。
    // ここが見たいのは配置なので、時刻の値そのものがパネルの中にあることで確かめる
    const panel = screen.getByText("残り時間").closest("section");
    expect(panel).not.toBeNull();
    expect(within(panel as HTMLElement).getByText("2:00")).toBeInTheDocument();
  });

  it("セッティングタイムには出さない", () => {
    // 準備中の操縦者の仕事は指差喚呼と動作確認だけ。まだ動いていない時計を
    // 置くと、答えるべき問いが 1 つ増える
    mount("setup", robotState(), { running: false, elapsed_ms: 0, duration_ms: 180_000 });

    expect(screen.queryByText("残り時間")).not.toBeInTheDocument();
  });
});

/**
 * 右カラム (試合時間 + 機体状態) は flex-col なので、**クロス軸は横**になる。
 * 子に `align-self` を付けると幅が `stretch` から `fit-content` へ変わり、
 * 列の幅ではなく内容が幅を決める —— 実際に `self-start` が付いていて、
 * 試合時間パネルは 157px まで縮んで `0:00` が枠からはみ出し、機体状態パネルは
 * 424px へ膨らんでビューポートを 58px 越えた (診断ツリーが画面外)。
 *
 * **flex-col の子は主軸 (縦) には伸びないので align-self は要らない。**
 * 内容ぶんの高さに留めるための `self-start` は grid の子の話 (grid なら
 * クロス軸が縦) で、ここへ書き写すと高さではなく幅を壊す。
 */
describe("試合中の右カラム", () => {
  const TIMERS: MatchState["timer"][] = [
    null,
    { running: true, elapsed_ms: 60_000, duration_ms: 180_000 },
  ];

  it("パネルの幅を列に追従させる — クロス軸の指定を持たない", () => {
    for (const timer of TIMERS) {
      const view = mount("match", robotState(), timer);

      const column = screen.getByText("残り時間").closest("section")?.parentElement;
      expect(column?.className).toContain("flex-col");

      const panels = Array.from(column?.children ?? []);
      // 機体状態が別の列へ移ったら、この検査は空振りになる
      expect(panels.some((el) => el.textContent?.includes("機体状態"))).toBe(true);

      const crossAxisPinned = panels.flatMap((el) =>
        Array.from(el.classList)
          .filter((name) => name.startsWith("self-"))
          .map((name) => `${el.querySelector("h2")?.textContent}: ${name}`),
      );
      expect(crossAxisPinned).toEqual([]);

      view.unmount();
    }
  });

  /**
   * 幅とは別の事実。**縦が足りないときにどちらが縮むか**を固定する。
   *
   * 機体状態は異常時に自分から展開するので、列の高さを超えるのは平常ではなく
   * 「何かが起きている最中」になる。そこで flex の既定 (`flex-shrink: 1`) に
   * 任せると両方が縮み、削れる余地の無い試合時間パネルは caption が数字の
   * 下半分に重なって読めなくなる (実機で発生)。試合中に最も参照する値なので、
   * 縮むのは内部スクロールを持つ機体状態の側でなければならない。
   */
  it("縦が足りないときは機体状態だけが縮む — 試合時間は潰さない", () => {
    // 強制展開させる。dry-run の操縦者画面が常にこの状態になる
    const view = mount(
      "match",
      robotState({
        safety: {
          sync_violations: [],
          unenergized_motors: ["rotate_l", "rotate_r"],
          firmware_unconfirmed_motors: [],
          failed_tasks: [],
          reenergizing: false,
          loops_running: true,
          monitors_running: true,
          refreshers_running: true,
          position_loops: [],
          sync_monitors: [],
          target_refreshers: [],
        },
      }),
      { running: true, elapsed_ms: 60_000, duration_ms: 180_000 },
    );
    expect(screen.getByRole("button", { expanded: true })).toBeInTheDocument();

    const timerPanel = screen.getByText("残り時間").closest("section");
    const statusPanel = screen.getByText("機体状態").closest("section");

    expect(timerPanel?.classList.contains("shrink-0")).toBe(true);
    // 機体状態は縮む側のまま。`min-h-0` が無いと中身の高さが下限になり、
    // 列を越えて伸びる (内部のモータ一覧のスクロールへ落ちない)
    expect(statusPanel?.classList.contains("shrink-0")).toBe(false);
    expect(statusPanel?.classList.contains("min-h-0")).toBe(true);
    // 縦を食い尽くす `flex-1` は付けない。平常時に全高の白い箱になる
    expect(statusPanel?.classList.contains("flex-1")).toBe(false);

    view.unmount();
  });
});

describe("RobotControl の操作先", () => {
  it("主操作はすべて自分の担当機へ宛てて送る", async () => {
    // 2 名の操縦者が別タブで同じ画面を開く。宛先を間違えると、押した本人の
    // 機体ではなくもう一方が動き出す。誤爆に気付くのは動いた後になる
    const { context } = mount("match");

    await userEvent.click(screen.getByRole("button", { name: "シーケンスを先頭から開始" }));
    expect(context.sendOrReport).toHaveBeenCalledWith(
      { type: "sequence_start", robot: "sub_hand" },
      expect.any(String),
    );
  });

  it("トリガーと通常停止も同じ宛先に揃える", async () => {
    const { context } = mount("match", robotState({ running: true, waiting_trigger: true }));

    await userEvent.click(screen.getByRole("button", { name: "次のステップへ進む" }));
    expect(context.sendOrReport).toHaveBeenCalledWith(
      { type: "trigger", robot: "sub_hand" },
      expect.any(String),
    );

    await userEvent.click(screen.getByRole("button", { name: "シーケンスを通常停止" }));
    expect(context.sendOrReport).toHaveBeenCalledWith(
      { type: "sequence_stop", robot: "sub_hand" },
      expect.any(String),
    );
  });

  it("再励磁も自分の担当機へ宛てて送る", async () => {
    // `_cmd_reenergize_motors` はロボット名が無い・未知なら拒否も返さず
    // 黙って return するので、宛先を間違えると「押せるが何も起きず、
    // 拒否トーストも出ない」という気付けない壊れ方になる
    const { context } = mount(
      "match",
      robotState({
        safety: {
          sync_violations: [],
          unenergized_motors: ["rotate_l"],
          firmware_unconfirmed_motors: [],
          failed_tasks: [],
          reenergizing: false,
          loops_running: true,
          monitors_running: true,
          refreshers_running: true,
          position_loops: [],
          sync_monitors: [],
          target_refreshers: [],
        },
      }),
    );

    await userEvent.click(screen.getByRole("button", { name: "再励磁" }));
    expect(context.sendOrReport).toHaveBeenCalledWith(
      { type: "reenergize_motors", robot: "sub_hand" },
      expect.any(String),
    );
  });

  it("ステップジャンプは確認を経てから宛先付きで送る", async () => {
    // 物理状態を確かめずに途中から再開すると機構をぶつける。確認なしで
    // 飛べる経路ができていないことも併せて守る
    const { context } = mount("match");

    await userEvent.click(screen.getByRole("button", { name: "ステップ 3: 搬送" }));
    expect(context.sendOrReport).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: "再開" }));
    expect(context.sendOrReport).toHaveBeenCalledWith(
      { type: "sequence_jump", robot: "sub_hand", step_index: 2 },
      expect.any(String),
    );
  });

  it("駆動中は「クリックで再開」と案内しない (押せないものを押せると言わない)", () => {
    // 一覧の行は素の <button disabled> で見た目がほとんど変わらないので、案内文が
    // 古いままだと操縦者には「押したのに反応しない」＝故障としか見えない
    mount("match", robotState({ running: true, step_index: 1 }));

    expect(screen.queryByText("クリックで再開")).toBeNull();
    expect(screen.getByText("停止してから選択")).toBeInTheDocument();
  });

  it("駆動中はステップジャンプの確認モーダルを開かない", async () => {
    // **機体が動いているあいだ画面を覆ってはならない。** ジャンプの確認は全画面
    // オーバーレイなので、開いているあいだヘッダーの EMG STOP がクリックできない
    // (クリックは背景として吸われてパネルが閉じるだけで、機体は止まらない)。
    // `MotorCheckPanel` をモーダルから外した理由と同じ形の事故。
    mount("match", robotState({ running: true, step_index: 1 }));

    await userEvent.click(screen.getByRole("button", { name: "ステップ 3: 搬送" }));

    expect(screen.queryByRole("button", { name: "再開" })).toBeNull();
    expect(document.querySelector(".modal")).toBeNull();
  });

  it("トリガー待ちではステップジャンプできる (機体は止まっている)", async () => {
    // `require_trigger` のステップで止まっている間は機体が動いていない。
    // そこは再開ステップを選ぶ本来の場面なので塞がない。
    //
    // **`running: true` を落とさないこと。** 実運用のトリガー待ちは実行中のまま
    // 止まっている状態 (`sequenceKind` が `waiting_trigger` を `running` より先に
    // 判定する) で、`running` を既定の false にすると、可否を `state.running === true`
    // で書いた実装 —— トリガー待ちまで塞いでしまう変異 —— でもこのテストが通る
    const { context } = mount(
      "match",
      robotState({ running: true, waiting_trigger: true, step_index: 1 }),
    );

    await userEvent.click(screen.getByRole("button", { name: "ステップ 3: 搬送" }));
    await userEvent.click(screen.getByRole("button", { name: "再開" }));

    expect(context.sendOrReport).toHaveBeenCalledWith(
      { type: "sequence_jump", robot: "sub_hand", step_index: 2 },
      expect.any(String),
    );
  });
});

describe("RobotControl のフェーズ別レイアウト", () => {
  it("準備中はシーケンス操作を出さない", () => {
    // このフェーズに操縦者の主操作は無い。押せない主操作ボタンを並べると
    // 「今やること」が埋もれる
    mount("setup");

    expect(screen.queryByRole("button", { name: "シーケンスを先頭から開始" })).toBeNull();
    expect(screen.queryByRole("button", { name: "シーケンスを通常停止" })).toBeNull();
  });

  it("指差喚呼と動作確認はこの画面に出さない (Monitor の設定面へ集約した)", () => {
    // 指差喚呼: 操縦者 2 名は同じ場所に立つので、2 画面に置くと二度読み上げになる。
    // 動作確認: 両ハンドを 1 本のシーケンスで駆動するので、機体ごとの入口が
    // あると 2 つを同時に起動できてしまう (両機が同時に動きうる)
    mount("setup");

    expect(screen.queryByText(/セッティング指差喚呼/)).toBeNull();
    expect(screen.queryByRole("button", { name: /動作確認を開始/ })).toBeNull();
  });

  it("試合中は主操作を出す", () => {
    mount("match");

    expect(screen.getByRole("button", { name: "シーケンスを先頭から開始" })).toBeEnabled();
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

    expect(context.sendOrReport).toHaveBeenCalledTimes(1);
    expect(context.sendOrReport).toHaveBeenCalledWith(
      { type: "sequence_start", robot: "sub_hand" },
      expect.any(String),
    );
  });

  it("許可待ちの Space は NEXT に解決する", () => {
    const { context } = mount("match", robotState({ running: true, waiting_trigger: true }));

    pressSpace();

    expect(context.sendOrReport).toHaveBeenCalledWith(
      { type: "trigger", robot: "sub_hand" },
      expect.any(String),
    );
  });

  it("実行中の Space は何も送らない (多重トリガーを作らない)", () => {
    const { context } = mount("match", robotState({ running: true }));

    pressSpace();

    expect(context.sendOrReport).not.toHaveBeenCalled();
  });

  it("準備中の Space は機体を動かさない", () => {
    // 指差喚呼中に机上でキーへ触れても機体が動いてはならない
    const { context } = mount("setup");

    pressSpace();

    expect(context.sendOrReport).not.toHaveBeenCalled();
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
          unenergized_motors: [],
          firmware_unconfirmed_motors: [],
          failed_tasks: [],
          reenergizing: false,
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

const MANUAL: ManualState = {
  mode: "manual",
  axes: [
    {
      name: "rotate",
      unit: "deg",
      command_mode: "position",
      value: 3,
      target: null,
      manual: { min: -5, max: 30, steps: [1, 5] },
      deviation: 0.1,
      sync_tolerance: 1.0,
      positions: [
        { name: "home", value: 0 },
        { name: "pick", value: 20 },
      ],
      motors: ["rotate_r", "rotate_l"],
    },
  ],
};

/** 手動モードで描く。モードの正はサーバー配信の state.manual.mode */
function mountManual(
  phase: MatchPhase,
  overrides: Partial<Parameters<typeof renderWithRobot>[1]> = {},
  manual: ManualState = MANUAL,
) {
  return renderWithRobot(<RobotControl robotKey="sub_hand" label="サブハンド" />, {
    states: { sub_hand: robotState({ manual }) },
    matchState: { ...DEFAULT_MATCH_STATE, phase, checklists: CHECKLISTS, timer: null },
    ...overrides,
  });
}

describe("手動操縦モード", () => {
  it("配信を受け取るまでは半自動として描く", () => {
    // 機体を直接動かせる状態を、確証のないまま画面へ出さない
    mount("match", robotState({ manual: undefined }));

    expect(screen.getByText("半自動")).toBeInTheDocument();
    expect(screen.queryByText(/手動操縦中/)).toBeNull();
  });

  it("モード帯はどのフェーズでも同じ位置に出る", () => {
    // 「今この画面から機体を直接動かせるか」は準備中も試合中も同じ場所で読める
    for (const phase of ["setup", "match", "finished"] as MatchPhase[]) {
      const view = mount(phase);
      expect(screen.getByRole("button", { name: /手動操縦へ/ })).toBeInTheDocument();
      view.unmount();
    }
  });

  it("切り替えは自分の担当機へ宛てて送る", async () => {
    const { context } = mount("match");

    await userEvent.click(screen.getByRole("button", { name: /手動操縦へ/ }));

    expect(context.sendOrReport).toHaveBeenCalledWith(
      { type: "set_operation_mode", robot: "sub_hand", mode: "manual" },
      expect.any(String),
    );
  });

  it("試合中は手動パネルがシーケンスの操作面を置き換える", () => {
    // 同じ列に 2 つの操作面が並ぶと、どちらの指令が機体へ届くのか読めなくなる
    mountManual("match");

    expect(screen.getByLabelText("rotate を 1deg 進める")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "シーケンスを先頭から開始" })).toBeNull();
  });

  it("準備中は手動パネルが指差喚呼を置き換える", () => {
    mountManual("setup");

    expect(screen.getByLabelText("rotate を home へ")).toBeInTheDocument();
    expect(screen.queryByText(/セッティング指差喚呼/)).toBeNull();
  });

  it("試合終了後も手動で動かせる", () => {
    // 退避や片付けは試合が終わってからのほうが多い
    mountManual("finished");

    expect(screen.getByLabelText("rotate を 1deg 進める")).toBeEnabled();
  });

  it("手動中は Space が sequence_start にならない", async () => {
    // 誤爆した Space でシーケンスが走り出すと、手動で動かしている機構とぶつかる
    const { context } = mountManual("match");

    pressSpace();

    expect(context.sendOrReport).not.toHaveBeenCalled();
  });

  it("緊急停止中は理由を出して指令を塞ぐ", async () => {
    const { context } = mountManual("match", { eStopActive: true });

    expect(screen.getAllByText("緊急停止中は手動操縦できません").length).toBeGreaterThan(0);
    await userEvent.click(screen.getByLabelText("rotate を home へ"));
    expect(context.send).not.toHaveBeenCalled();
  });

  it("緊急停止中でもモード切替は送れる", async () => {
    // 停止中に画面を手動へ寄せ、解除と同時に動かす手順を塞ぐ理由が無い
    const { context } = mountManual(
      "match",
      { eStopActive: true },
      { mode: "sequence", axes: MANUAL.axes },
    );

    await userEvent.click(screen.getByRole("button", { name: /手動操縦へ/ }));

    expect(context.sendOrReport).toHaveBeenCalledWith(
      { type: "set_operation_mode", robot: "sub_hand", mode: "manual" },
      expect.any(String),
    );
  });

  // 手動中に動作確認を塞ぐことは Monitor 側 (MotorCheckButton) が受け持つ。
  // サーバーが `blocked_reason` に理由を載せて配るので、画面はそれを出すだけ

  it("手動中は機体状態を畳まない", () => {
    // 機体を直接動かしている最中は「操縦者は機体を見ており画面は一瞬しか見ない」
    // という前提が成り立たない
    mountManual("match");

    expect(screen.getByRole("button", { expanded: true })).toBeInTheDocument();
  });
});

/**
 * `sequence_stop` は `step_index` を保持したまま降りるので、画面は「2/3・現在
 * ステップ○○」を出したままになる。そこで押す START (と Space 1 打) は**ステップ 0 へ
 * 戻って全工程を走り直す** —— 中断姿勢のまま先頭の動作が走る。同じ「任意ステップから
 * 再開」である `sequence_jump` には確認モーダルと「物理状態が安全であることを必ず
 * 確認してください」があるのに、より危険なこちらだけが素通しだった。
 */
describe("中断位置から押す START", () => {
  const stopped = () => robotState({ step_index: 1, running: false });

  it("確認を経てから sequence_start を送る", async () => {
    const { context } = mount("match", stopped());

    await userEvent.click(screen.getByRole("button", { name: "シーケンスを先頭から再開" }));
    expect(context.sendOrReport).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: "先頭から実行" }));
    expect(context.sendOrReport).toHaveBeenCalledWith(
      { type: "sequence_start", robot: "sub_hand" },
      expect.any(String),
    );
  });

  it("何が起きるかと、途中から再開する手段を書く", async () => {
    mount("match", stopped());

    await userEvent.click(screen.getByRole("button", { name: "シーケンスを先頭から再開" }));

    expect(screen.getByText(/全工程を走り直します/)).toBeInTheDocument();
    // これを書かないと、操縦者は他に手が無いと思って全工程のやり直しを選ぶ
    expect(screen.getByText(/ステップ一覧から再開するステップを選んで/)).toBeInTheDocument();
    expect(screen.getByText(/物理状態が安全であることを必ず確認/)).toBeInTheDocument();
  });

  it("キャンセルすれば 1 通も送らない", async () => {
    const { context } = mount("match", stopped());

    await userEvent.click(screen.getByRole("button", { name: "シーケンスを先頭から再開" }));
    await userEvent.click(screen.getByRole("button", { name: "キャンセル" }));

    expect(context.sendOrReport).not.toHaveBeenCalled();
  });

  it("Space 1 打では走り出さない (キーの方がボタンより危ない)", () => {
    const { context } = mount("match", stopped());

    act(() => pressSpace());

    expect(context.sendOrReport).not.toHaveBeenCalled();
    // 無反応で終わらせない。同じ確認を出して次の一手を示す
    expect(screen.getByText(/全工程を走り直します/)).toBeInTheDocument();
  });

  it("一度も走っていない状態は確認を挟まない (試合開始直後の 1 回目)", async () => {
    const { context } = mount("match", robotState({ step_index: 0, running: false }));

    await userEvent.click(screen.getByRole("button", { name: "シーケンスを先頭から開始" }));

    expect(context.sendOrReport).toHaveBeenCalledWith(
      { type: "sequence_start", robot: "sub_hand" },
      expect.any(String),
    );
  });
});

/**
 * `send` は切断中に false を返すだけなので、塞がないと「押したのにボタンは有効な
 * まま・機体は動かない・トーストも出ない」になる。手動操縦・動作確認・コート選択・
 * StartGate は最初から `connected` を見ており、**主操作だけが例外**だった。
 */
describe("RobotControl の切断中", () => {
  it("主操作を押せなくし、理由を出す", () => {
    // `mount` の既定は connected: true なので、切断は明示的に組む
    const view = renderWithRobot(<RobotControl robotKey="sub_hand" label="サブハンド" />, {
      connected: false,
      states: { sub_hand: robotState({ running: true, waiting_trigger: true }) },
      matchState: { ...DEFAULT_MATCH_STATE, phase: "match", checklists: CHECKLISTS },
    });

    expect(screen.queryByRole("button", { name: "次のステップへ進む" })).toBeNull();
    expect(view.container.querySelectorAll("button[disabled]").length).toBeGreaterThan(0);
    expect(screen.getAllByText("切断中のため送信できません").length).toBeGreaterThan(0);
  });

  it("ステップジャンプも押せなくする", () => {
    renderWithRobot(<RobotControl robotKey="sub_hand" label="サブハンド" />, {
      connected: false,
      states: { sub_hand: robotState() },
      matchState: { ...DEFAULT_MATCH_STATE, phase: "match", checklists: CHECKLISTS },
    });

    expect(screen.getByRole("button", { name: "ステップ 3: 搬送" })).toBeDisabled();
  });
});

function stepPanel(): HTMLElement {
  const panel = screen.getByText("ステップ").closest("section");
  if (!panel) throw new Error("ステップ一覧のパネルが見つからない");
  return panel as HTMLElement;
}

/**
 * 見出し行に出すのは**塞がれている理由**だけ。操作できるときの案内は押せば分かる
 * ことを毎試合読ませるだけだが、**塞がれている理由は消してはならない** ——
 * 消すと「押したのに何も起きない」だけが操縦者に残る。
 */
describe("ステップ一覧の見出し", () => {
  it("操作できるときは案内を出さない", () => {
    mount("match");

    expect(within(stepPanel()).queryByText("クリックで再開")).toBeNull();
  });

  it("試合中でなければ、塞がれている理由を出す", () => {
    mount("finished");

    expect(within(stepPanel()).getByText("試合中のみ操作可")).toBeInTheDocument();
  });

  it("切断中も、塞がれている理由を出す", () => {
    renderWithRobot(<RobotControl robotKey="sub_hand" label="サブハンド" />, {
      connected: false,
      states: { sub_hand: robotState() },
      matchState: { ...DEFAULT_MATCH_STATE, phase: "match", checklists: CHECKLISTS },
    });

    expect(within(stepPanel()).getByText("切断中のため送信できません")).toBeInTheDocument();
  });
});

/**
 * シーケンス名と総ステップ数はモード帯が持つ。1 行の事実にパネル枠 1 つぶんの縦を
 * 払わない。総ステップ数は**ステップ一覧が画面に無いときだけ**帯が引き受ける ——
 * 半自動では準備中に一覧が、試合中に `ActionPanel` の `1/22` が同じ数を既に
 * 出している (同じ事実を 2 度描かない)。
 */
describe("シーケンス名の置き場所", () => {
  it("準備中はモード帯にシーケンス名を出す", () => {
    mount("setup");

    expect(screen.getByText("sub_hand")).toBeInTheDocument();
    // 1 行のためのパネルは持たない
    expect(screen.queryByText("シーケンス")).toBeNull();
  });

  it("半自動では総ステップ数を出さない (準備中は一覧が、試合中は ActionPanel が出す)", () => {
    mount("setup");
    expect(screen.queryByText(/全 3 ステップ/)).toBeNull();

    mount("match");
    expect(screen.queryByText(/全 3 ステップ/)).toBeNull();
  });

  it("手動の準備中だけ帯が総ステップ数を引き受ける (一覧が画面から消えるため)", () => {
    mountManual("setup");

    expect(screen.getByText(/全 3 ステップ/)).toBeInTheDocument();
  });
});

/**
 * 準備中の操縦者画面は、かつて機体状態 1 枚が全幅へ広がるだけだった ——
 * このフェーズで「これから何が起きるか」を答える面が画面のどこにも無く、
 * 操縦者は手順を確認する手段を持たないまま試合開始を待っていた。
 */
describe("準備中のステップ一覧", () => {
  it("半自動の準備中にも一覧を出す", () => {
    mount("setup");

    expect(screen.getByRole("button", { name: "ステップ 3: 搬送" })).toBeInTheDocument();
  });

  it("押せなくし、その理由を出す", () => {
    // `sequence_jump` はサーバー側でフェーズゲートされる。押せる見た目にすると
    // 「押したのに何も起きない」だけが操縦者に残る
    mount("setup");

    expect(screen.getByRole("button", { name: "ステップ 3: 搬送" })).toBeDisabled();
    expect(within(stepPanel()).getByText("試合中のみ操作可")).toBeInTheDocument();
  });

  it("手動中は出さない (同じ列を手元の操作面へ明け渡す)", () => {
    mountManual("setup");

    expect(screen.queryByRole("button", { name: "ステップ 3: 搬送" })).toBeNull();
  });
});
