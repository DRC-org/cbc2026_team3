import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { MotorCheckPanel } from "@/components/motorcheck/MotorCheckPanel";
import { RobotProvider } from "@/context/RobotContext";
import type { MotorCheckSnapshot, SequenceStepInfo } from "@/lib/protocol";
import { MALFORMED } from "@/lib/protocol";
import { createRobotContext, EMPTY_MOTOR_CHECK, renderWithRobot } from "@/test/robotContext";

const STEPS: SequenceStepInfo[] = [
  { index: 0, label: "メインハンド 初期姿勢へ", require_trigger: false },
  { index: 1, label: "メインハンド y 軸 (左右直結ペア)", require_trigger: false },
  { index: 2, label: "サブハンド 電磁弁 6 個 (打音・目視確認)", require_trigger: false },
];

const TOGGLE = { name: "手順と結果" };

function mount(check: Partial<MotorCheckSnapshot> = {}, connected = true) {
  return renderWithRobot(<MotorCheckPanel />, {
    connected,
    motorCheck: {
      ...EMPTY_MOTOR_CHECK,
      available: true,
      blocked_reason: null,
      steps: STEPS,
      total_steps: STEPS.length,
      ...check,
    },
  });
}

describe("MotorCheckPanel", () => {
  it("画面を覆わない (モーダルではない)", () => {
    // **これがこの部品の存在理由。** モーダルだった頃は駆動しているあいだずっと
    // 全画面オーバーレイがヘッダーを覆い、EMG STOP がクリックできなかった
    mount({ running: true, step_index: 1, current_step: STEPS[1].label });

    expect(document.querySelector(".modal")).toBeNull();
  });

  it("何を動かすかをステップ一覧で読める", async () => {
    // 押す前に「両機の何がどの順で動くか」が読めないと、周囲の安全確認ができない。
    // 平常時は畳んでおく (指差喚呼 12 項目の上に 15 行が常時居座らないように)
    mount();
    expect(screen.queryByText("メインハンド 初期姿勢へ")).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", TOGGLE));

    expect(screen.getByText("メインハンド 初期姿勢へ")).toBeInTheDocument();
    expect(screen.getByText("サブハンド 電磁弁 6 個 (打音・目視確認)")).toBeInTheDocument();
  });

  it("実行中は畳んでいても自分から開き、今どのステップかを出す", () => {
    // 畳んだまま機体だけが動く画面を作らない (`SubsystemStatus` と同じ方針)
    mount({ running: true, step_index: 1, current_step: STEPS[1].label });

    expect(screen.getByRole("button", TOGGLE)).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("1 / 3")).toBeInTheDocument();
    expect(screen.getAllByText("メインハンド y 軸 (左右直結ペア)").length).toBeGreaterThan(0);
    expect(screen.getByText("実行中")).toBeInTheDocument();
  });

  it("中断・失敗の理由は畳んでいても自分から開いて出す", () => {
    mount({ error: "緊急停止中のため動作確認を中止しました" });

    expect(screen.getByRole("button", TOGGLE)).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("動作確認は完了していません")).toBeInTheDocument();
    expect(screen.getByText("緊急停止中のため動作確認を中止しました")).toBeInTheDocument();
  });

  it("合否の列を持たない", async () => {
    // 到達判定を持たない軸 (duty / on_off) に「合格」を出すと、動いたかどうかを
    // 機械が見ていないのに見たように読めてしまう
    mount({ running: false, step_index: 3 });
    await userEvent.click(screen.getByRole("button", TOGGLE));

    expect(screen.queryByText("合格")).not.toBeInTheDocument();
    expect(screen.queryByText(/期待/)).not.toBeInTheDocument();
  });

  it("実行中は中断できる", async () => {
    const { context } = mount({ running: true, step_index: 1 });

    await userEvent.click(screen.getByRole("button", { name: "中断" }));
    expect(context.send).toHaveBeenCalledWith({ type: "motor_check_abort" });
  });

  it("実行中は操縦者の操作でも畳めない (畳んだまま機体だけが動く画面を作らない)", async () => {
    mount({ running: true, step_index: 1 });

    await userEvent.click(screen.getByRole("button", TOGGLE));

    expect(screen.getByRole("button", TOGGLE)).toHaveAttribute("aria-expanded", "true");
    // 止める操作は開閉の外側にあり、どちらの状態でも同じ位置に出る
    expect(screen.getByRole("button", { name: "中断" })).toBeInTheDocument();
  });

  it("実行が終わったら畳んだ状態へ戻る (開きっぱなしにしない)", async () => {
    // 強制開示中の反転を (v) => !v で書くと、見た目は開いたままなのに内部だけ
    // 「開く」へ倒れ、終わった後もステップ 15 行が指差喚呼の上に居座り続ける
    const running: MotorCheckSnapshot = {
      ...EMPTY_MOTOR_CHECK,
      available: true,
      blocked_reason: null,
      steps: STEPS,
      total_steps: STEPS.length,
      running: true,
      step_index: 1,
    };
    const context = createRobotContext({ motorCheck: running });
    const view = render(
      <RobotProvider value={context}>
        <MotorCheckPanel />
      </RobotProvider>,
    );

    await userEvent.click(screen.getByRole("button", TOGGLE));
    view.rerender(
      <RobotProvider
        value={{
          ...context,
          motorCheck: { ...running, running: false, step_index: STEPS.length },
        }}
      >
        <MotorCheckPanel />
      </RobotProvider>,
    );

    expect(screen.getByRole("button", TOGGLE)).toHaveAttribute("aria-expanded", "false");
  });

  it("起動ボタンを持たない (入口は MotorCheckButton の 1 つだけ)", () => {
    // インラインになったので同じ区分の中に並ぶ。ここにも置くと同じ操作が 2 つ並ぶ
    mount({ running: false, step_index: 3 });

    expect(screen.queryByRole("button", { name: /実行/ })).not.toBeInTheDocument();
  });

  /**
   * **既定の mount() がまさにこの状態** (running:false, step_index:0, ステップ表あり)。
   * 実配信のスナップショットもこの形で、以前は全ステップに緑の ✓ が付いていた。
   *
   * `config/checklist.yaml` の「アクチュエータ動作確認 完了」は、この誤表示のまま
   * チェックが付く経路になっていた。
   */
  it("未実行を完了と表示しない", async () => {
    mount();
    await userEvent.click(screen.getByRole("button", TOGGLE));

    // ✓ が付いた行が 1 つも無いこと (走っていないのに通過済みには見せない)
    expect(document.querySelectorAll(".text-success")).toHaveLength(0);
  });

  it("完走したときだけ通過済みにする", async () => {
    mount({ step_index: STEPS.length });
    await userEvent.click(screen.getByRole("button", TOGGLE));

    expect(document.querySelectorAll(".text-success")).toHaveLength(STEPS.length);
  });

  it("起動できない構成ではその旨を出す", async () => {
    mount({
      available: false,
      steps: [],
      total_steps: 0,
      blocked_reason: "動作確認シーケンスが読み込まれていません",
    });
    await userEvent.click(screen.getByRole("button", TOGGLE));

    expect(screen.getByText(/この構成では動作確認を実行できません/)).toBeInTheDocument();
  });
});

/**
 * 内訳を出すのはここだけ。件数 1 語は `MotorCheckSummary` が区分見出しに常時出すので、
 * 畳んでいるあいだも「除外がある」ことは画面から読める。
 */
describe("MotorCheckPanel の除外表示", () => {
  it("除外したステップと欠けている軸を出す", async () => {
    // **黙って減らしてはならない。** 出さないと、サブハンド不在で減っているのか、
    // 本番構成なのに config の書き忘れで減っているのかを操縦者が区別できない
    // (どちらも「全ステップ成功」として同じに見える)
    mount({
      excluded_steps: [
        { step: "サブハンド 昇降", missing_axes: ["sub_lift"] },
        { step: "サブハンド 吸気・排気ポンプ (聴音確認)", missing_axes: ["pump_blow", "pump_vac"] },
      ],
    });
    await userEvent.click(screen.getByRole("button", TOGGLE));

    expect(screen.getByText(/2 件除外/)).toBeInTheDocument();
    expect(screen.getByText("サブハンド 昇降")).toBeInTheDocument();
    expect(screen.getByText(/sub_lift/)).toBeInTheDocument();
    expect(screen.getByText(/pump_blow, pump_vac/)).toBeInTheDocument();
  });

  it("除外が無ければ何も出さない", async () => {
    mount({ excluded_steps: [] });
    await userEvent.click(screen.getByRole("button", TOGGLE));

    expect(screen.queryByText(/除外/)).not.toBeInTheDocument();
  });

  it("読めない配信は「除外なし」に見せず判定不能として出す", async () => {
    mount({ excluded_steps: MALFORMED });
    await userEvent.click(screen.getByRole("button", TOGGLE));

    expect(screen.getByText("除外ステップを読み取れませんでした")).toBeInTheDocument();
  });
});
