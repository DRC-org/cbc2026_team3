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
  manual_always: false,
  deviation: 0.2,
  sync_tolerance: 2.0,
  positions: [
    { name: "home", value: 0 },
    { name: "work", value: 15 },
  ],
  motors: ["y_axis_r", "y_axis_l"],
};

const GRIPPER: ManualAxis = {
  name: "gripper",
  unit: "deg",
  command_mode: "position",
  value: 5,
  target: null,
  manual: null,
  manual_always: false,
  deviation: null,
  sync_tolerance: null,
  positions: [
    { name: "open", value: 5 },
    { name: "closed", value: 0 },
  ],
  motors: ["gripper"],
};

const VALVE: ManualAxis = {
  name: "valve_1",
  unit: "on_off",
  command_mode: "on_off",
  value: null,
  target: 0,
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

const MANUAL: ManualState = { mode: "manual", axes: [Y_AXIS, GRIPPER] };

const TWO_STEERABLE: ManualState = {
  mode: "manual",
  axes: [Y_AXIS, { ...Y_AXIS, name: "rotate", unit: "deg", motors: ["rotate_r", "rotate_l"] }],
};

function renderPanel(
  manual: ManualState = MANUAL,
  blockedReason: string | null = null,
  excludeAxes?: readonly string[],
) {
  const send = vi.fn(() => true);
  render(
    <ManualPanel
      robotKey="main_hand"
      manual={manual}
      blockedReason={blockedReason}
      sendOrReport={send}
      excludeAxes={excludeAxes}
    />,
  );
  return { send };
}

describe("ManualPanel", () => {
  it("配信された軸をそのまま並べる", () => {
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

    expect(send).toHaveBeenCalledWith(
      {
        type: "manual_jog",
        robot: "main_hand",
        axis: "y_axis",
        delta: 0.5,
      },
      "ジョグ",
    );
  });

  it("絶対値は manual_set を送る", async () => {
    const user = userEvent.setup();
    const { send } = renderPanel();
    const input = screen.getByLabelText("y_axis の目標値");

    await user.clear(input);
    await user.type(input, "7");
    await user.click(screen.getByLabelText("y_axis を入力値へ移動"));

    expect(send).toHaveBeenCalledWith(
      {
        type: "manual_set",
        robot: "main_hand",
        axis: "y_axis",
        value: 7,
      },
      "目標値の送信",
    );
  });

  it("プリセットは manual_move を送る", async () => {
    const user = userEvent.setup();
    const { send } = renderPanel();

    await user.click(screen.getByLabelText("gripper を open へ"));

    expect(send).toHaveBeenCalledWith(
      {
        type: "manual_move",
        robot: "main_hand",
        axis: "gripper",
        position: "open",
      },
      "プリセット移動",
    );
  });

  it("操作できないときは理由を出して 1 通も送らせない", async () => {
    const user = userEvent.setup();
    const { send } = renderPanel(MANUAL, "緊急停止中");

    expect(screen.getByText("緊急停止中")).toBeInTheDocument();
    await user.click(screen.getByLabelText("gripper を open へ"));
    expect(send).not.toHaveBeenCalled();
  });

  it("on_off 軸は行ではなく丸トグルの群に出す", () => {
    renderPanel({ mode: "manual", axes: [Y_AXIS, GRIPPER, VALVE] });

    expect(screen.getByLabelText("valve_1 を ON にする")).toBeInTheDocument();
    expect(screen.queryByLabelText("valve_1 を open へ")).toBeNull();
  });

  it("ON 側 / OFF 側のどちらかが欠けた on_off 軸は群に出さず、通常の行として出す", () => {
    const broken = { ...VALVE, positions: [{ name: "open", value: 1 }] };
    renderPanel({ mode: "manual", axes: [Y_AXIS, broken] });

    expect(screen.queryByLabelText("valve_1 を ON にする")).toBeNull();
    expect(screen.getByLabelText("valve_1 を open へ")).toBeInTheDocument();
  });

  describe("excludeAxes", () => {
    const WITH_VALVE: ManualState = { mode: "manual", axes: [Y_AXIS, GRIPPER, VALVE] };

    it("渡さなければ弁も群に並ぶ", () => {
      renderPanel(WITH_VALVE);

      expect(screen.getByLabelText("valve_1 を ON にする")).toBeInTheDocument();
    });

    it("渡した軸は群にも行にも出さない", () => {
      renderPanel(WITH_VALVE, null, ["valve_1", "gripper"]);

      expect(screen.queryByLabelText("valve_1 を ON にする")).toBeNull();
      expect(screen.queryByText("valve_1")).toBeNull();
      expect(screen.queryByText("gripper")).toBeNull();
      expect(screen.getByText("y_axis")).toBeInTheDocument();
    });

    it("除いた軸はキーボードの操作対象にも入らない", async () => {
      const user = userEvent.setup();
      const { send } = renderPanel(TWO_STEERABLE, null, ["y_axis"]);

      await user.keyboard("{ArrowRight}");

      expect(send).toHaveBeenCalledWith(
        { type: "manual_jog", robot: "main_hand", axis: "rotate", delta: 0.5 },
        "ジョグ",
      );
    });
  });

  it("軸が 1 つも無ければチップで言う", () => {
    renderPanel({ mode: "manual", axes: [] });
    expect(screen.getByText("手動軸なし")).toBeInTheDocument();
  });

  describe("キーボードの操作対象", () => {
    it("既定では先頭の連続軸が選ばれている", async () => {
      const user = userEvent.setup();
      const { send } = renderPanel();

      await user.keyboard("{ArrowRight}");

      expect(send).toHaveBeenCalledWith(
        {
          type: "manual_jog",
          robot: "main_hand",
          axis: "y_axis",
          delta: 0.5,
        },
        "ジョグ",
      );
    });

    it("↑ ↓ で操作対象が移る", async () => {
      const user = userEvent.setup();
      const { send } = renderPanel(TWO_STEERABLE);

      await user.keyboard("{ArrowDown}");
      await user.keyboard("{ArrowRight}");

      expect(send).toHaveBeenLastCalledWith(
        {
          type: "manual_jog",
          robot: "main_hand",
          axis: "rotate",
          delta: 0.5,
        },
        "ジョグ",
      );
    });

    it("端では選択が止まる (巡回しない)", async () => {
      const user = userEvent.setup();
      const { send } = renderPanel(TWO_STEERABLE);

      await user.keyboard("{ArrowUp}{ArrowUp}{ArrowUp}");
      await user.keyboard("{ArrowRight}");

      expect(send).toHaveBeenLastCalledWith(
        {
          type: "manual_jog",
          robot: "main_hand",
          axis: "y_axis",
          delta: 0.5,
        },
        "ジョグ",
      );
    });

    it("連続操作できない軸は選択対象に入らない", async () => {
      const user = userEvent.setup();
      const { send } = renderPanel();

      await user.keyboard("{ArrowDown}{ArrowDown}");
      await user.keyboard("{ArrowRight}");

      expect(send).toHaveBeenLastCalledWith(
        {
          type: "manual_jog",
          robot: "main_hand",
          axis: "y_axis",
          delta: 0.5,
        },
        "ジョグ",
      );
    });

    it("行を触るとその軸が操作対象になる", async () => {
      const user = userEvent.setup();
      const { send } = renderPanel(TWO_STEERABLE);

      await user.click(screen.getByText("rotate"));
      await user.keyboard("{ArrowRight}");

      expect(send).toHaveBeenLastCalledWith(
        {
          type: "manual_jog",
          robot: "main_hand",
          axis: "rotate",
          delta: 0.5,
        },
        "ジョグ",
      );
    });

    it("キーの割り当てを画面に出す", () => {
      renderPanel();
      for (const key of ["↑", "↓", "←", "→", "[", "]", "Home", "End"]) {
        expect(screen.getByText(key)).toBeInTheDocument();
      }
    });

    it("凡例に文章を添えない (クランプはサーバーが持つ)", () => {
      renderPanel();
      expect(document.body.textContent ?? "").not.toMatch(/可動範囲内でのみ/);
    });

    it("連続操作できる軸が無ければ凡例を出さない", () => {
      renderPanel({ mode: "manual", axes: [GRIPPER] });
      expect(screen.queryByText("Home")).toBeNull();
    });
  });
});
