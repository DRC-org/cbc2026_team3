import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { SwitchMeasurePanel } from "@/components/homing/SwitchMeasurePanel";
import { MALFORMED } from "@/lib/protocol";
import type { SwitchMeasureSnapshot, SwitchMeasurement } from "@/lib/protocol";
import { EMPTY_SWITCH_MEASURE, renderWithRobot } from "@/test/robotContext";

const TARGETS = { main_hand: ["y_axis"], sub_hand: ["sub_y_axis"] };

const RESULT: SwitchMeasurement = {
  axis: "sub_y_axis",
  unit: "mm",
  direction: -1,
  engage: -447.5,
  release: -441.2,
  width: 6.3,
  step: 0.5,
  coarse_step: 2,
};

function mount(
  switchMeasure: Partial<SwitchMeasureSnapshot> = {},
  extra: Record<string, unknown> = {},
) {
  return renderWithRobot(<SwitchMeasurePanel />, {
    connected: true,
    switchMeasure: {
      ...EMPTY_SWITCH_MEASURE,
      available: true,
      blocked_reason: null,
      targets: TARGETS,
      ...switchMeasure,
    },
    ...extra,
  });
}

const START_BUTTON = { name: "作動点測定を開始" };

async function chooseSubHandMinus() {
  const robot = screen.getByRole("combobox", { name: "測定するロボット" });
  await userEvent.selectOptions(robot, "sub_hand");
  await userEvent.click(screen.getByRole("button", { name: "- 方向" }));
}

function resultRow(label: string) {
  return within(screen.getByRole("table")).getByText(label).closest("tr");
}

describe("SwitchMeasurePanel", () => {
  it("向きを選ぶまでは開始できない", () => {
    mount();

    expect(screen.getByRole("button", START_BUTTON)).toBeDisabled();
    expect(screen.getByText("向きを選んでください")).toBeInTheDocument();
  });

  it("確認にロボット・軸・向きが出る", async () => {
    mount();

    await chooseSubHandMinus();
    await userEvent.click(screen.getByRole("button", START_BUTTON));

    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText("サブハンドだけ")).toBeInTheDocument();
    expect(within(dialog).getAllByText("sub_y_axis").length).toBeGreaterThan(0);
    expect(within(dialog).getByText("- 方向")).toBeInTheDocument();
    expect(within(dialog).getByText(/人・物がないことを確認/)).toBeInTheDocument();
  });

  it("空欄の任意項目はキーごと落として送る", async () => {
    const sendOrReport = vi.fn(() => true);
    mount({}, { sendOrReport });

    await chooseSubHandMinus();
    await userEvent.click(screen.getByRole("button", START_BUTTON));
    await userEvent.click(screen.getByRole("button", { name: "開始" }));

    expect(sendOrReport).toHaveBeenCalledWith(
      { type: "switch_measure_start", robot: "sub_hand", axis: "sub_y_axis", direction: -1 },
      "作動点測定の開始",
    );
  });

  it("入力した任意項目は正の数で送る", async () => {
    const sendOrReport = vi.fn(() => true);
    mount({}, { sendOrReport });

    await chooseSubHandMinus();
    await userEvent.type(screen.getByRole("spinbutton", { name: "刻み" }), "0.5");
    await userEvent.type(screen.getByRole("spinbutton", { name: "粗刻み" }), "2");
    await userEvent.type(screen.getByRole("spinbutton", { name: "上限" }), "60");
    await userEvent.click(screen.getByRole("button", START_BUTTON));
    await userEvent.click(screen.getByRole("button", { name: "開始" }));

    expect(sendOrReport).toHaveBeenCalledWith(
      {
        type: "switch_measure_start",
        robot: "sub_hand",
        axis: "sub_y_axis",
        direction: -1,
        step: 0.5,
        coarse_step: 2,
        limit: 60,
      },
      "作動点測定の開始",
    );
  });

  it("キャンセルしたら送らない", async () => {
    const sendOrReport = vi.fn(() => true);
    mount({}, { sendOrReport });

    await chooseSubHandMinus();
    await userEvent.click(screen.getByRole("button", START_BUTTON));
    await userEvent.click(screen.getByRole("button", { name: "キャンセル" }));

    expect(sendOrReport).not.toHaveBeenCalled();
  });

  it("結果表に作動点・離脱点・ON 区間・刻みを単位付きで出す", () => {
    mount({ robot: "sub_hand", axis: "sub_y_axis", direction: -1, result: RESULT });

    expect(screen.getByText("完了")).toBeInTheDocument();
    expect(resultRow("作動点")).toHaveTextContent("-447.5 mm");
    expect(resultRow("離脱点")).toHaveTextContent("-441.2 mm");
    expect(resultRow("ON 区間")).toHaveTextContent("6.3 mm");
    expect(resultRow("刻み")).toHaveTextContent("0.5 mm");
    expect(resultRow("刻み")).toHaveTextContent("粗刻み 2 mm");
  });

  it("結果を読み取れなければ黙って空にしない", () => {
    mount({ robot: "sub_hand", axis: "sub_y_axis", direction: -1, result: MALFORMED });

    expect(screen.getByText("失敗")).toBeInTheDocument();
    expect(screen.getByText(/結果を読み取れませんでした/)).toBeInTheDocument();
  });

  it("塞ぐ理由はサーバーの blocked_reason をそのまま出す", async () => {
    mount({ blocked_reason: "零点合わせの実行中は作動点測定を実行できません" });

    await chooseSubHandMinus();

    expect(screen.getByRole("button", START_BUTTON)).toBeDisabled();
    expect(screen.getByText("零点合わせの実行中は作動点測定を実行できません")).toBeInTheDocument();
  });

  it("失敗理由はサーバーの error をそのまま出す", () => {
    mount({
      robot: "sub_hand",
      axis: "sub_y_axis",
      direction: -1,
      error: "上限まで動いてもスイッチが入りませんでした",
    });

    expect(screen.getByText("失敗")).toBeInTheDocument();
    expect(screen.getByText("上限まで動いてもスイッチが入りませんでした")).toBeInTheDocument();
  });

  it("測定中はその対象を出し、入力を触れなくする", () => {
    mount({ running: true, robot: "main_hand", axis: "y_axis", direction: 1 });

    expect(screen.getByText("測定中")).toBeInTheDocument();
    expect(screen.getByText("+ 方向", { selector: "span" })).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "測定する軸" })).toBeDisabled();
  });

  it("切断中は押せない", async () => {
    renderWithRobot(<SwitchMeasurePanel />, {
      connected: false,
      switchMeasure: {
        ...EMPTY_SWITCH_MEASURE,
        available: true,
        blocked_reason: null,
        targets: TARGETS,
      },
    });

    await chooseSubHandMinus();

    expect(screen.getByRole("button", START_BUTTON)).toBeDisabled();
    expect(screen.getByText("切断中のため不可")).toBeInTheDocument();
  });

  it("対象を読み取れなければ入力を出さずに知らせる", () => {
    mount({ targets: MALFORMED });

    expect(screen.queryByRole("button", START_BUTTON)).not.toBeInTheDocument();
    expect(screen.getByText(/対象を読み取れませんでした/)).toBeInTheDocument();
  });
});
