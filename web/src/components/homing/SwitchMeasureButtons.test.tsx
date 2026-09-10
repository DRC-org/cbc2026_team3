import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { SwitchMeasureButtons } from "@/components/homing/SwitchMeasureButtons";
import { MALFORMED } from "@/lib/protocol";
import type { RobotState, SwitchMeasureSnapshot, SwitchMeasurement } from "@/lib/protocol";
import { EMPTY_SWITCH_MEASURE, renderWithRobot } from "@/test/robotContext";

const TARGETS = { main_hand: ["y_axis"], sub_hand: ["sub_y_axis", "sub_lift"] };

const RESULT: SwitchMeasurement = {
  axis: "sub_lift",
  unit: "mm",
  direction: 1,
  engage: 159.0,
  release: 158.2,
  width: 0.8,
  step: 0.1,
  coarse_step: 0.5,
};

function robotIn(mode: "sequence" | "manual"): RobotState {
  return { manual: { mode, axes: [] } } as unknown as RobotState;
}

function mount(
  switchMeasure: Partial<SwitchMeasureSnapshot> = {},
  extra: Record<string, unknown> = {},
) {
  return renderWithRobot(<SwitchMeasureButtons robot="sub_hand" />, {
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

const LIFT_PLUS = { name: "sub_lift の+ 方向の作動点測定を開始" };

function axisRow(axis: string) {
  const row = screen.getByText(axis).closest("li");
  if (row === null) throw new Error(`${axis} の行が無い`);
  return row;
}

describe("SwitchMeasureButtons", () => {
  it("配信された軸 × 向きごとに 1 つずつ出し、他機の軸は出さない", () => {
    mount();

    expect(screen.getAllByRole("button")).toHaveLength(4);
    expect(
      screen.getByRole("button", { name: "sub_y_axis の- 方向の作動点測定を開始" }),
    ).toBeEnabled();
    expect(screen.getByRole("button", LIFT_PLUS)).toBeEnabled();
    expect(screen.queryByText("y_axis")).toBeNull();
    expect(screen.queryByRole("combobox")).toBeNull();
    expect(screen.queryByRole("spinbutton")).toBeNull();
  });

  it("確認にロボット・軸・向きが出て、刻みは homing の既定のまま送る", async () => {
    const sendOrReport = vi.fn(() => true);
    mount({}, { sendOrReport });

    await userEvent.click(screen.getByRole("button", LIFT_PLUS));

    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText("サブハンドだけ")).toBeInTheDocument();
    expect(within(dialog).getByText("sub_lift")).toBeInTheDocument();
    expect(within(dialog).getByText("+ 方向")).toBeInTheDocument();
    expect(within(dialog).getByText(/人・物がないことを確認/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "開始" }));

    expect(sendOrReport).toHaveBeenLastCalledWith(
      { type: "switch_measure_start", robot: "sub_hand", axis: "sub_lift", direction: 1 },
      "作動点測定の開始",
    );
  });

  it("キャンセルしたら送らない", async () => {
    const sendOrReport = vi.fn(() => true);
    mount({}, { sendOrReport });

    await userEvent.click(screen.getByRole("button", LIFT_PLUS));
    await userEvent.click(screen.getByRole("button", { name: "キャンセル" }));

    expect(sendOrReport).not.toHaveBeenCalled();
  });

  it("結果は測定した軸の行の下に出す", () => {
    mount({ robot: "sub_hand", axis: "sub_lift", direction: 1, result: RESULT });

    const lift = axisRow("sub_lift");
    expect(within(lift).getByText("完了")).toBeInTheDocument();
    expect(within(lift).getByRole("table")).toHaveTextContent("159 mm");
    expect(within(axisRow("sub_y_axis")).queryByRole("table")).toBeNull();
  });

  it("他機の結果は出さない", () => {
    mount({
      robot: "main_hand",
      axis: "y_axis",
      direction: 1,
      result: { ...RESULT, axis: "y_axis" },
    });

    expect(screen.queryByRole("table")).toBeNull();
    expect(screen.queryByText("完了")).toBeNull();
  });

  it("失敗理由と読めなかった結果は黙って空にしない", () => {
    const view = mount({
      robot: "sub_hand",
      axis: "sub_y_axis",
      direction: -1,
      error: "上限まで動いてもスイッチが入りませんでした",
    });
    expect(within(axisRow("sub_y_axis")).getByText("失敗")).toBeInTheDocument();
    expect(screen.getByText("上限まで動いてもスイッチが入りませんでした")).toBeInTheDocument();
    view.unmount();

    mount({ robot: "sub_hand", axis: "sub_y_axis", direction: -1, result: MALFORMED });
    expect(screen.getByText(/結果を読み取れませんでした/)).toBeInTheDocument();
  });

  it("測定中は押した向きにだけスピナーを出す", () => {
    mount({ running: true, robot: "sub_hand", axis: "sub_lift", direction: 1 });

    expect(within(axisRow("sub_lift")).getByText("測定中")).toBeInTheDocument();
    expect(screen.getByRole("button", LIFT_PLUS).querySelector(".loading")).not.toBeNull();
    expect(
      screen
        .getByRole("button", { name: "sub_lift の- 方向の作動点測定を開始" })
        .querySelector(".loading"),
    ).toBeNull();
  });

  it("塞ぐ理由はサーバーの blocked_reason をそのまま出す", () => {
    mount({ blocked_reason: "零点合わせの実行中は作動点測定を実行できません" });

    expect(screen.getByRole("button", LIFT_PLUS)).toBeDisabled();
    expect(screen.getByText("零点合わせの実行中は作動点測定を実行できません")).toBeInTheDocument();
  });

  it("切断中は押せない", () => {
    mount({}, { connected: false });

    expect(screen.getByRole("button", LIFT_PLUS)).toBeDisabled();
    expect(screen.getByText("切断中のため不可")).toBeInTheDocument();
  });

  it("担当機に対象が無ければボタンを出さずに知らせる", () => {
    mount({ targets: { main_hand: ["y_axis"] } });

    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.getByText("作動点を測定できる軸がありません。")).toBeInTheDocument();
  });

  it("対象を読み取れなければボタンを出さずに知らせる", () => {
    mount({ targets: MALFORMED });

    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.getByText(/対象を読み取れませんでした/)).toBeInTheDocument();
  });

  describe("手動操縦中", () => {
    const STATES = { main_hand: robotIn("manual"), sub_hand: robotIn("sequence") };
    const REASON = "'main_hand' が手動操縦モードのため作動点測定を実行できません";

    it("サーバーの拒否理由では塞がず、全機を半自動へ戻してから送る", async () => {
      const sendOrReport = vi.fn<(data: unknown, what: string) => boolean>(() => true);
      mount({ blocked_reason: REASON }, { states: STATES, sendOrReport });

      expect(screen.getByRole("button", LIFT_PLUS)).toBeEnabled();
      expect(screen.queryByText(REASON)).toBeNull();

      await userEvent.click(screen.getByRole("button", LIFT_PLUS));
      expect(screen.getByText(/全機を半自動へ戻してから開始/)).toBeInTheDocument();
      await userEvent.click(screen.getByRole("button", { name: "開始" }));

      expect(sendOrReport.mock.calls.map(([data]) => data)).toEqual([
        { type: "set_operation_mode", robot: "main_hand", mode: "sequence" },
        { type: "set_operation_mode", robot: "sub_hand", mode: "sequence" },
        { type: "switch_measure_start", robot: "sub_hand", axis: "sub_lift", direction: 1 },
      ]);
    });

    it("半自動へ戻せなければ作動点測定を送らない", async () => {
      const sendOrReport = vi.fn(() => false);
      mount({ blocked_reason: REASON }, { states: STATES, sendOrReport });

      await userEvent.click(screen.getByRole("button", LIFT_PLUS));
      await userEvent.click(screen.getByRole("button", { name: "開始" }));

      expect(sendOrReport).not.toHaveBeenCalledWith(
        expect.objectContaining({ type: "switch_measure_start" }),
        expect.any(String),
      );
    });

    it("緊急停止中は手動でも塞ぐ", () => {
      mount(
        { blocked_reason: "緊急停止中のため作動点測定を実行できません" },
        { states: STATES, eStopActive: true },
      );

      expect(screen.getByRole("button", LIFT_PLUS)).toBeDisabled();
    });
  });
});
