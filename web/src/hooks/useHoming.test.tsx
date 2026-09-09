import { renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { RobotProvider } from "@/context/RobotContext";
import { useHoming } from "@/hooks/useHoming";
import { createRobotContext } from "@/test/robotContext";
import type { RobotContextValue } from "@/test/robotContext";

function mount(overrides: Partial<RobotContextValue> = {}) {
  const context = createRobotContext(overrides);
  const view = renderHook(() => useHoming(), {
    wrapper: ({ children }) => <RobotProvider value={context}>{children}</RobotProvider>,
  });
  return { ...view, context };
}

describe("useHoming", () => {
  it("受信前は起動できない状態を返す", () => {
    const { result } = mount();

    expect(result.current.state.available).toBe(false);
    expect(result.current.state.blocked_reason).not.toBeNull();
  });

  it("start は必ず robot を載せる", () => {
    const sendOrReport = vi.fn(() => true);
    const { result } = mount({ sendOrReport });

    result.current.start("sub_hand", ["sub_y_axis"]);

    expect(sendOrReport).toHaveBeenCalledWith(
      { type: "homing_start", robot: "sub_hand", axes: ["sub_y_axis"] },
      "零点合わせの開始",
    );
  });

  it("軸を省いたらそのロボットの全軸 (axes を載せない)", () => {
    const sendOrReport = vi.fn(() => true);
    const { result } = mount({ sendOrReport });

    result.current.start("main_hand");

    expect(sendOrReport).toHaveBeenCalledWith(
      { type: "homing_start", robot: "main_hand" },
      "零点合わせの開始",
    );
  });

  it("素の send を使わない (送れなかった 1 回を捨てない)", () => {
    const send = vi.fn(() => true);
    const { result } = mount({ send });

    result.current.start("sub_hand");

    expect(send).not.toHaveBeenCalled();
  });
});
