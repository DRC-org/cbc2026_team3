import { renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { RobotProvider } from "@/context/RobotContext";
import { useMotorCheck } from "@/hooks/useMotorCheck";
import type { MotorCheckState } from "@/hooks/useRobotSocket";
import { createRobotContext } from "@/test/robotContext";
import type { RobotContextValue } from "@/test/robotContext";

function mount(robot: string, overrides: Partial<RobotContextValue> = {}) {
  const context = createRobotContext(overrides);
  const view = renderHook(() => useMotorCheck(robot), {
    wrapper: ({ children }) => <RobotProvider value={context}>{children}</RobotProvider>,
  });
  return { ...view, context };
}

describe("useMotorCheck", () => {
  it("未実施のロボットには idle の既定状態を返す", () => {
    const { result } = mount("main_hand");

    expect(result.current.state).toMatchObject({
      status: "idle",
      current: null,
      progress: null,
      records: [],
      error: null,
    });
  });

  it("該当ロボットの状態だけを取り出す", () => {
    const running: MotorCheckState = {
      status: "running",
      current: "lift",
      progress: { index: 1, total: 3 },
      records: [],
      snapshot: null,
      error: null,
      startedAtMs: 100_000,
      finishedAtMs: null,
    };
    const { result } = mount("main_hand", {
      motorChecks: { main_hand: running, sub_hand: { ...running, current: "other" } },
    });

    expect(result.current.state.current).toBe("lift");
  });

  it("start で対象ロボットの開始コマンドを送る", () => {
    const send = vi.fn();
    const { result } = mount("sub_hand", { send });

    result.current.start();
    expect(send).toHaveBeenCalledWith({ type: "motor_check_start", robot: "sub_hand" });
  });

  it("abort で中断コマンドを送る", () => {
    const send = vi.fn();
    const { result } = mount("sub_hand", { send });

    result.current.abort();
    expect(send).toHaveBeenCalledWith({ type: "motor_check_abort", robot: "sub_hand" });
  });

  it("再レンダーしても start/abort の参照が変わらない", () => {
    const { result, rerender } = mount("main_hand");
    const before = result.current;

    rerender();
    expect(result.current.start).toBe(before.start);
    expect(result.current.abort).toBe(before.abort);
  });
});
