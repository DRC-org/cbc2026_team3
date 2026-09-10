import { renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { RobotProvider } from "@/context/RobotContext";
import { useSequenceModeRestore } from "@/hooks/useSequenceModeRestore";
import type { RobotState } from "@/lib/protocol";
import { createRobotContext } from "@/test/robotContext";
import type { RobotContextValue } from "@/test/robotContext";

function robot(mode: "sequence" | "manual" | undefined): RobotState {
  return { manual: mode === undefined ? undefined : { mode, axes: [] } } as unknown as RobotState;
}

function mount(overrides: Partial<RobotContextValue> = {}) {
  const context = createRobotContext(overrides);
  const view = renderHook(() => useSequenceModeRestore(), {
    wrapper: ({ children }) => <RobotProvider value={context}>{children}</RobotProvider>,
  });
  return { ...view, context };
}

describe("useSequenceModeRestore", () => {
  it("どれか 1 機でも手動なら anyManual", () => {
    const { result } = mount({
      states: { main_hand: robot("manual"), sub_hand: robot("sequence") },
    });

    expect(result.current.anyManual).toBe(true);
  });

  it("全機が半自動 (または未配信) なら anyManual ではない", () => {
    const { result } = mount({
      states: { main_hand: robot("sequence"), sub_hand: robot(undefined) },
    });

    expect(result.current.anyManual).toBe(false);
  });

  it("restore は知っているロボット全部へ半自動を送る (既に半自動でも)", () => {
    const sendOrReport = vi.fn(() => true);
    const { result } = mount({
      states: { main_hand: robot("sequence"), sub_hand: robot("manual") },
      sendOrReport,
    });

    expect(result.current.restore()).toBe(true);

    expect(sendOrReport.mock.calls).toEqual([
      [{ type: "set_operation_mode", robot: "main_hand", mode: "sequence" }, "半自動への復帰"],
      [{ type: "set_operation_mode", robot: "sub_hand", mode: "sequence" }, "半自動への復帰"],
    ]);
  });

  it("1 通でも送れなければ false (残りは送る)", () => {
    const sendOrReport = vi.fn().mockReturnValueOnce(false).mockReturnValue(true);
    const { result } = mount({
      states: { main_hand: robot("manual"), sub_hand: robot("manual") },
      sendOrReport,
    });

    expect(result.current.restore()).toBe(false);
    expect(sendOrReport).toHaveBeenCalledTimes(2);
  });

  it("素の send を使わない (送れなかった 1 回を捨てない)", () => {
    const send = vi.fn(() => true);
    const { result } = mount({ states: { sub_hand: robot("manual") }, send });

    result.current.restore();

    expect(send).not.toHaveBeenCalled();
  });
});
