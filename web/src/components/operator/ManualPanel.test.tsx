import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ManualPanel } from "@/components/operator/ManualPanel";
import type { ManualState } from "@/lib/protocol";

const MANUAL: ManualState = {
  mode: "manual",
  axes: [
    {
      name: "y_axis",
      unit: "mm",
      command_mode: "position",
      value: 5,
      target: 4,
      manual: { min: -2, max: 20, steps: [0.5, 2] },
      positions: ["home", "work"],
      motors: ["y_axis_r", "y_axis_l"],
    },
    {
      name: "gripper",
      unit: "deg",
      command_mode: "position",
      value: 5,
      target: null,
      manual: null,
      positions: ["open", "closed"],
      motors: ["gripper"],
    },
  ],
};

function renderPanel(manual: ManualState = MANUAL, blockedReason: string | null = null) {
  const send = vi.fn(() => true);
  render(
    <ManualPanel robotKey="main_hand" manual={manual} blockedReason={blockedReason} send={send} />,
  );
  return { send };
}

describe("ManualPanel", () => {
  it("配信された軸をそのまま並べる", () => {
    // 軸名を UI 側へ書かないことで、機構が変わっても UI を触らずに済む
    renderPanel();
    expect(screen.getByText("y_axis")).toBeInTheDocument();
    expect(screen.getByText("gripper")).toBeInTheDocument();
  });

  it("軸が増えれば行も増える (UI 側にハードコードが無い)", () => {
    const extra = {
      ...MANUAL,
      axes: [...MANUAL.axes, { ...MANUAL.axes[1], name: "wall_f", motors: ["wall_f"] }],
    };
    renderPanel(extra);
    expect(screen.getByText("wall_f")).toBeInTheDocument();
    expect(screen.getByLabelText("wall_f を open へ")).toBeInTheDocument();
  });

  it("ジョグは manual_jog を軸名付きで送る", async () => {
    const user = userEvent.setup();
    const { send } = renderPanel();

    await user.click(screen.getByLabelText("y_axis を 0.5mm 進める"));

    expect(send).toHaveBeenCalledWith({
      type: "manual_jog",
      robot: "main_hand",
      axis: "y_axis",
      delta: 0.5,
    });
  });

  it("絶対値は manual_set を送る", async () => {
    const user = userEvent.setup();
    const { send } = renderPanel();

    await user.type(screen.getByLabelText("y_axis の目標値"), "7");
    await user.click(screen.getByLabelText("y_axis を入力値へ移動"));

    expect(send).toHaveBeenCalledWith({
      type: "manual_set",
      robot: "main_hand",
      axis: "y_axis",
      value: 7,
    });
  });

  it("プリセットは manual_move を送る", async () => {
    const user = userEvent.setup();
    const { send } = renderPanel();

    await user.click(screen.getByLabelText("gripper を open へ"));

    expect(send).toHaveBeenCalledWith({
      type: "manual_move",
      robot: "main_hand",
      axis: "gripper",
      position: "open",
    });
  });

  it("操作できないときは理由を出して 1 通も送らせない", async () => {
    const user = userEvent.setup();
    const { send } = renderPanel(MANUAL, "緊急停止中は手動操縦できません");

    expect(screen.getByText("緊急停止中は手動操縦できません")).toBeInTheDocument();
    await user.click(screen.getByLabelText("gripper を open へ"));
    expect(send).not.toHaveBeenCalled();
  });

  it("軸が 1 つも無ければ理由を説明する", () => {
    // 位置定数を読めていないロボットで空の操作面だけが出ると、
    // 「壊れている」のか「そういうものなのか」が画面から分からない
    renderPanel({ mode: "manual", axes: [] });
    expect(screen.getByText(/手動操縦できる軸がありません/)).toBeInTheDocument();
  });
});
