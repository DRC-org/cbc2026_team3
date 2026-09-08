import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider, createMemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { MotorCheckPanel } from "@/components/motorcheck/MotorCheckPanel";
import { RobotProvider } from "@/context/RobotContext";
import type { MotorCheckSnapshot, SequenceStepInfo } from "@/lib/protocol";
import { MALFORMED } from "@/lib/protocol";
import { installMockWebSocket, latestSocket } from "@/test/mockWebSocket";
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
    mount({ running: true, step_index: 1, current_step: STEPS[1].label });

    expect(document.querySelector(".modal")).toBeNull();
  });

  it("何を動かすかをステップ一覧で読める", async () => {
    mount();
    expect(screen.queryByText("メインハンド 初期姿勢へ")).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", TOGGLE));

    expect(screen.getByText("メインハンド 初期姿勢へ")).toBeInTheDocument();
    expect(screen.getByText("サブハンド 電磁弁 6 個 (打音・目視確認)")).toBeInTheDocument();
  });

  it("実行中は畳んでいても自分から開き、今どのステップかを出す", () => {
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
    mount({ running: false, step_index: 3 });
    await userEvent.click(screen.getByRole("button", TOGGLE));

    expect(screen.queryByText("合格")).not.toBeInTheDocument();
    expect(screen.queryByText(/期待/)).not.toBeInTheDocument();
  });

  it("実行中は中断できる", async () => {
    const { context } = mount({ running: true, step_index: 1 });

    await userEvent.click(screen.getByRole("button", { name: "中断" }));
    expect(context.sendOrReport).toHaveBeenCalledWith(
      { type: "motor_check_abort" },
      "動作確認の中断",
    );
  });

  it("実行中は操縦者の操作でも畳めない (畳んだまま機体だけが動く画面を作らない)", async () => {
    mount({ running: true, step_index: 1 });

    await userEvent.click(screen.getByRole("button", TOGGLE));

    expect(screen.getByRole("button", TOGGLE)).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("button", { name: "中断" })).toBeInTheDocument();
  });

  it("実行が終わったら畳んだ状態へ戻る (開きっぱなしにしない)", async () => {
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
    mount({ running: false, step_index: 3 });

    expect(screen.queryByRole("button", { name: /実行/ })).not.toBeInTheDocument();
  });

  it("未実行を完了と表示しない", async () => {
    mount();
    await userEvent.click(screen.getByRole("button", TOGGLE));

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

  it("ステップ一覧が読めなかったことを平常の文言に紛れさせない", async () => {
    mount({ steps: MALFORMED });
    await userEvent.click(screen.getByRole("button", TOGGLE));

    expect(screen.getByText(/ステップ一覧を読み取れませんでした/)).toBeInTheDocument();
    expect(screen.queryByText(/ステップが読み込まれていません/)).not.toBeInTheDocument();
  });
});

describe("MotorCheckPanel の除外表示", () => {
  it("除外したステップと欠けている軸を出す", async () => {
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

describe("切断中の動作確認の中断", () => {
  beforeEach(() => {
    installMockWebSocket();
  });

  it("送れなかったことを操縦者へ伝える", async () => {
    const { routes } = await import("@/routes");
    const router = createMemoryRouter(routes, { initialEntries: ["/monitor"] });
    render(<RouterProvider router={router} />);

    act(() => latestSocket().open());
    act(() =>
      latestSocket().receive({
        type: "motor_check_state",
        available: true,
        blocked_reason: null,
        running: true,
        step_index: 1,
        current_step: STEPS[1].label,
        steps: STEPS,
        total_steps: STEPS.length,
        excluded_steps: [],
        error: null,
        last_error: null,
      }),
    );
    act(() => latestSocket().close());

    await userEvent.click(screen.getByRole("button", { name: "中断" }));

    expect(screen.getByText(/動作確認の中断を送信できませんでした/)).toBeInTheDocument();
  });
});

describe("MotorCheckPanel の引き寄せ", () => {
  function watchScroll() {
    return vi.spyOn(Element.prototype, "scrollIntoView").mockImplementation(() => {});
  }

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("実行中は開いた先を視界へ引き寄せる", () => {
    const scrollIntoView = watchScroll();

    mount({ running: true, step_index: 1, current_step: STEPS[1].label });

    expect(scrollIntoView).toHaveBeenCalledWith({ block: "start", behavior: "smooth" });
  });

  it("失敗したときも引き寄せる", () => {
    const scrollIntoView = watchScroll();

    mount({ error: "ステップ '零点確定' で失敗しました" });

    expect(scrollIntoView).toHaveBeenCalled();
  });

  it("失敗のあと再実行しても引き寄せる", () => {
    const scrollIntoView = watchScroll();
    const panel = (check: Partial<MotorCheckSnapshot>) => (
      <RobotProvider
        value={createRobotContext({
          motorCheck: { ...EMPTY_MOTOR_CHECK, available: true, steps: STEPS, ...check },
        })}
      >
        <MotorCheckPanel />
      </RobotProvider>
    );

    const { rerender } = render(panel({ error: "ステップ '零点確定' で失敗しました" }));
    expect(scrollIntoView).toHaveBeenCalledTimes(1);

    rerender(panel({ running: true, step_index: 0, current_step: STEPS[0].label }));

    expect(scrollIntoView).toHaveBeenCalledTimes(2);
  });

  it("平常時は動かさない", () => {
    const scrollIntoView = watchScroll();

    mount();

    expect(scrollIntoView).not.toHaveBeenCalled();
  });
});
