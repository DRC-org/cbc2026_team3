import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ManualPanel } from "@/components/operator/ManualPanel";
import type { ManualAxis, ManualState } from "@/lib/protocol";

const Y_AXIS: ManualAxis = {
  name: "y_axis",
  unit: "mm",
  command_mode: "position",
  value: 5,
  target: 4,
  manual: { min: -2, max: 20, steps: [0.5, 2] },
  deviation: 0.2,
  sync_tolerance: 2.0,
  positions: ["home", "work"],
  motors: ["y_axis_r", "y_axis_l"],
};

const GRIPPER: ManualAxis = {
  name: "gripper",
  unit: "deg",
  command_mode: "position",
  value: 5,
  target: null,
  manual: null,
  deviation: null,
  sync_tolerance: null,
  positions: ["open", "closed"],
  motors: ["gripper"],
};

const MANUAL: ManualState = { mode: "manual", axes: [Y_AXIS, GRIPPER] };

/** 連続操作できる軸が 2 本ある構成。軸選択の移動を見るために要る */
const TWO_STEERABLE: ManualState = {
  mode: "manual",
  axes: [Y_AXIS, { ...Y_AXIS, name: "rotate", unit: "deg", motors: ["rotate_r", "rotate_l"] }],
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
      axes: [...MANUAL.axes, { ...GRIPPER, name: "wall_f", motors: ["wall_f"] }],
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
    const input = screen.getByLabelText("y_axis の目標値");

    await user.clear(input);
    await user.type(input, "7");
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

  describe("キーボードの操作対象", () => {
    it("既定では先頭の連続軸が選ばれている", async () => {
      const user = userEvent.setup();
      const { send } = renderPanel();

      await user.keyboard("{ArrowRight}");

      expect(send).toHaveBeenCalledWith({
        type: "manual_jog",
        robot: "main_hand",
        axis: "y_axis",
        delta: 0.5,
      });
    });

    it("↑ ↓ で操作対象が移る", async () => {
      const user = userEvent.setup();
      const { send } = renderPanel(TWO_STEERABLE);

      await user.keyboard("{ArrowDown}");
      await user.keyboard("{ArrowRight}");

      expect(send).toHaveBeenLastCalledWith({
        type: "manual_jog",
        robot: "main_hand",
        axis: "rotate",
        delta: 0.5,
      });
    });

    it("端では選択が止まる (巡回しない)", async () => {
      // 巡回すると、下端で 1 回押しすぎただけで先頭の軸へ飛ぶ。
      // どの軸を操作しているかは画面を見ずに把握できる必要がある
      const user = userEvent.setup();
      const { send } = renderPanel(TWO_STEERABLE);

      await user.keyboard("{ArrowUp}{ArrowUp}{ArrowUp}");
      await user.keyboard("{ArrowRight}");

      expect(send).toHaveBeenLastCalledWith({
        type: "manual_jog",
        robot: "main_hand",
        axis: "y_axis",
        delta: 0.5,
      });
    });

    it("連続操作できない軸は選択対象に入らない", async () => {
      // ← → が何も起こさない行へ降りられると、キーが効かないのか
      // 軸が動かないのかを画面から区別できない
      const user = userEvent.setup();
      const { send } = renderPanel();

      await user.keyboard("{ArrowDown}{ArrowDown}");
      await user.keyboard("{ArrowRight}");

      expect(send).toHaveBeenLastCalledWith({
        type: "manual_jog",
        robot: "main_hand",
        axis: "y_axis",
        delta: 0.5,
      });
    });

    it("行を触るとその軸が操作対象になる", async () => {
      const user = userEvent.setup();
      const { send } = renderPanel(TWO_STEERABLE);

      await user.click(screen.getByText("rotate"));
      await user.keyboard("{ArrowRight}");

      expect(send).toHaveBeenLastCalledWith({
        type: "manual_jog",
        robot: "main_hand",
        axis: "rotate",
        delta: 0.5,
      });
    });

    it("キーの割り当てを画面に出す", () => {
      // 凡例と実装が別の場所にあると必ず食い違う
      renderPanel();
      for (const key of ["↑", "↓", "←", "→", "[", "]", "Home", "End"]) {
        expect(screen.getByText(key)).toBeInTheDocument();
      }
    });

    it("連続操作できる軸が無ければ凡例を出さない", () => {
      renderPanel({ mode: "manual", axes: [GRIPPER] });
      expect(screen.queryByText("Home")).toBeNull();
    });
  });
});
