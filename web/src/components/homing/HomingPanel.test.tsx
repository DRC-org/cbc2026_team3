import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { HomingPanel } from "@/components/homing/HomingPanel";
import { MALFORMED } from "@/lib/protocol";
import type { HomingSnapshot } from "@/lib/protocol";
import { EMPTY_HOMING, renderWithRobot } from "@/test/robotContext";

function mount(homing: Partial<HomingSnapshot>) {
  return renderWithRobot(<HomingPanel />, {
    connected: true,
    homing: { ...EMPTY_HOMING, available: true, blocked_reason: null, ...homing },
  });
}

describe("HomingPanel", () => {
  it("実行前は何も出さない", () => {
    const { container } = mount({});
    expect(container).toBeEmptyDOMElement();
  });

  it("実行中は宛先のロボットと今の軸を出す", () => {
    mount({ running: true, robot: "sub_hand", axes: ["sub_y_axis"], current_axis: "sub_y_axis" });

    expect(screen.getByText("サブハンド")).toBeInTheDocument();
    expect(screen.getByText("実行中")).toBeInTheDocument();
    expect(screen.getByText("sub_y_axis")).toBeInTheDocument();
  });

  it("失敗した軸は理由ごと出す", () => {
    mount({
      robot: "sub_hand",
      axes: ["sub_y_axis"],
      results: [{ axis: "sub_y_axis", error: "原点センサに到達しませんでした" }],
    });

    expect(screen.getByText("未完了")).toBeInTheDocument();
    expect(screen.getByText("原点センサに到達しませんでした")).toBeInTheDocument();
  });

  it("全て通れば完了として出す", () => {
    mount({ robot: "main_hand", axes: ["y_axis"], results: [{ axis: "y_axis", error: null }] });

    expect(screen.getByText("完了")).toBeInTheDocument();
    expect(screen.getByText("y_axis")).toBeInTheDocument();
  });

  it("結果を読み取れなければ黙って空にしない", () => {
    mount({ robot: "sub_hand", results: MALFORMED });

    expect(screen.getByText(/読み取れませんでした/)).toBeInTheDocument();
  });
});
