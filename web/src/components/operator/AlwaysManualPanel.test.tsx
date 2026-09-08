import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { AlwaysManualPanel } from "@/components/operator/AlwaysManualPanel";
import type { ManualAxis, ManualState } from "@/lib/protocol";

/** シーケンス制御中でも操作できる軸 (config が `manual_always: true` を宣言した duty 軸) */
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

/** 宣言していない軸。シーケンスが到達判定に使うので、手動では触らせない */
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

function renderPanel(axes: ManualAxis[], blockedReason: string | null = null) {
  // 半自動のまま出す面なので、配信の `mode` は `sequence`。**モードでは絞らない**
  // (絞るのは呼び出し元の RobotControl 側で、手動中はこの面ごと出さない)
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
    // 対象の正は config だけが持つ。UI へ軸名を書かないので、増減しても無変更で済む
    renderPanel([Y_AXIS, CONVEYOR]);

    expect(screen.getByText("conveyor")).toBeInTheDocument();
    expect(screen.queryByText("y_axis")).toBeNull();
  });

  it("欄が欠けた配信では 1 つも出さない", () => {
    // 版ずれで `manual_always` が落ちた配信を「押してよい」と読むと、シーケンス
    // 実行中に押せるボタンが勝手に増える。読む側は `=== true` で厳密に見る
    const { manual_always: _dropped, ...withoutField } = CONVEYOR;
    const { view } = renderPanel([withoutField as ManualAxis]);

    expect(view.container).toBeEmptyDOMElement();
  });

  it("対象軸が無ければパネルごと描かない", () => {
    // 空の枠が常に置かれていると、その機体に何か操作できるものがあるように読める
    const { view } = renderPanel([Y_AXIS]);

    expect(view.container).toBeEmptyDOMElement();
  });

  it("プリセットは manual_move として自分の担当機へ宛てて送る", async () => {
    // 値ではなく名前で送る。数値を送る経路を作った時点で
    // 「定義した状態以外を送れない」保証が消える
    const { send } = renderPanel([CONVEYOR]);

    await userEvent.click(screen.getByLabelText("conveyor を stop へ"));

    expect(send).toHaveBeenCalledWith(
      { type: "manual_move", robot: "main_hand", axis: "conveyor", position: "stop" },
      expect.any(String),
    );
  });

  it("塞がれているときは理由を出してボタンを無効にする", async () => {
    // 可否の正はサーバー。ここは押す前に理由を出すだけで、判定は増やさない
    const { send } = renderPanel([CONVEYOR], "緊急停止中は手動操縦できません");

    expect(screen.getByText("緊急停止中は手動操縦できません")).toBeInTheDocument();
    expect(screen.getByLabelText("conveyor を run へ")).toBeDisabled();
    await userEvent.click(screen.getByLabelText("conveyor を run へ"));
    expect(send).not.toHaveBeenCalled();
  });

  it("シーケンスに上書きされることを断る", () => {
    // 断っておかないと「押したのに戻った」が故障に見える
    renderPanel([CONVEYOR]);

    expect(screen.getByText(/上書きされます/)).toBeInTheDocument();
  });
});
