import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { AlwaysManualPanel } from "@/components/operator/AlwaysManualPanel";
import type { ManualAxis, ManualState } from "@/lib/protocol";

const CONVEYOR: ManualAxis = {
  name: "conveyor",
  unit: "duty",
  command_mode: "duty",
  value: null,
  target: null,
  manual: null,
  manual_always: true,
  deviation: null,
  sync_tolerance: null,
  positions: [
    { name: "stop", value: 0 },
    { name: "run", value: 0.3 },
  ],
  motors: ["conveyor"],
};

const Y_AXIS: ManualAxis = {
  name: "y_axis",
  unit: "mm",
  command_mode: "position",
  value: 5,
  target: 4,
  manual: { min: -2, max: 20, steps: [0.5, 2] },
  manual_always: false,
  deviation: 0.2,
  sync_tolerance: 2.0,
  positions: [
    { name: "home", value: 0 },
    { name: "work", value: 15 },
  ],
  motors: ["y_axis_r", "y_axis_l"],
};

const VALVE: ManualAxis = {
  name: "valve_1",
  unit: "on_off",
  command_mode: "on_off",
  value: null,
  target: 1,
  manual: null,
  manual_always: true,
  deviation: null,
  sync_tolerance: null,
  positions: [
    { name: "closed", value: 0 },
    { name: "open", value: 1 },
  ],
  motors: ["valve_1"],
};

function renderPanel(axes: ManualAxis[], blockedReason: string | null = null) {
  const manual: ManualState = { mode: "sequence", axes };
  const send = vi.fn(() => true);
  const view = render(
    <AlwaysManualPanel
      robotKey="main_hand"
      manual={manual}
      blockedReason={blockedReason}
      sendOrReport={send}
    />,
  );
  return { send, view };
}

describe("AlwaysManualPanel", () => {
  it("manual_always を宣言した軸だけを並べる", () => {
    renderPanel([Y_AXIS, CONVEYOR]);

    expect(screen.getByText("conveyor")).toBeInTheDocument();
    expect(screen.queryByText("y_axis")).toBeNull();
  });

  it("欄が欠けた配信では 1 つも出さない", () => {
    const { manual_always: _dropped, ...withoutField } = CONVEYOR;
    const { view } = renderPanel([withoutField as ManualAxis]);

    expect(view.container).toBeEmptyDOMElement();
  });

  it("対象軸が無ければパネルごと描かない", () => {
    const { view } = renderPanel([Y_AXIS]);

    expect(view.container).toBeEmptyDOMElement();
  });

  it("プリセットは manual_move として自分の担当機へ宛てて送る", async () => {
    const { send } = renderPanel([CONVEYOR]);

    await userEvent.click(screen.getByLabelText("conveyor を stop へ"));

    expect(send).toHaveBeenCalledWith(
      { type: "manual_move", robot: "main_hand", axis: "conveyor", position: "stop" },
      expect.any(String),
    );
  });

  it("塞がれているときは理由を出してボタンを無効にする", async () => {
    const { send } = renderPanel([CONVEYOR], "緊急停止中は手動操縦できません");

    expect(screen.getByText("緊急停止中は手動操縦できません")).toBeInTheDocument();
    expect(screen.getByLabelText("conveyor を run へ")).toBeDisabled();
    await userEvent.click(screen.getByLabelText("conveyor を run へ"));
    expect(send).not.toHaveBeenCalled();
  });

  it("on_off 軸は行ではなく丸トグルの群に出す", () => {
    renderPanel([CONVEYOR, VALVE]);

    expect(screen.getByLabelText("valve_1 を OFF にする")).toBeInTheDocument();
    expect(screen.queryByLabelText("valve_1 を closed へ")).toBeNull();
    expect(screen.getByLabelText("conveyor を stop へ")).toBeInTheDocument();
  });

  it("丸トグルも manual_move として自分の担当機へ宛てて送る", async () => {
    const { send } = renderPanel([VALVE]);

    await userEvent.click(screen.getByLabelText("valve_1 を OFF にする"));

    expect(send).toHaveBeenCalledWith(
      { type: "manual_move", robot: "main_hand", axis: "valve_1", position: "closed" },
      expect.any(String),
    );
  });

  it("シーケンスに上書きされることを断る", () => {
    renderPanel([CONVEYOR]);

    expect(screen.getByText(/上書きされます/)).toBeInTheDocument();
  });
});
