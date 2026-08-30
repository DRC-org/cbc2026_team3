import { renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { RobotProvider } from "@/context/RobotContext";
import { useMotorCheck } from "@/hooks/useMotorCheck";
import type { MotorCheckSnapshot } from "@/lib/protocol";
import { createRobotContext } from "@/test/robotContext";
import type { RobotContextValue } from "@/test/robotContext";

function mount(overrides: Partial<RobotContextValue> = {}) {
  const context = createRobotContext(overrides);
  const view = renderHook(() => useMotorCheck(), {
    wrapper: ({ children }) => <RobotProvider value={context}>{children}</RobotProvider>,
  });
  return { ...view, context };
}

const RUNNING: MotorCheckSnapshot = {
  available: true,
  blocked_reason: "既に動作確認を実行中です",
  running: true,
  current_step: "メインハンド y 軸 (左右直結ペア)",
  step_index: 1,
  total_steps: 3,
  steps: [
    { index: 0, label: "メインハンド 初期姿勢へ", require_trigger: false },
    { index: 1, label: "メインハンド y 軸 (左右直結ペア)", require_trigger: false },
    { index: 2, label: "両ハンドを初期姿勢へ戻す", require_trigger: false },
  ],
  error: null,
};

describe("useMotorCheck", () => {
  it("受信前は起動できない状態を返す", () => {
    // 「起動できる」へ倒すと、配信が届く前の一瞬だけ押せるボタンが出る
    const { result } = mount();

    expect(result.current.state.available).toBe(false);
    expect(result.current.state.blocked_reason).not.toBeNull();
    expect(result.current.state.running).toBe(false);
  });

  it("サーバーが配った状態をそのまま返す", () => {
    const { result } = mount({ motorCheck: RUNNING });

    expect(result.current.state.current_step).toBe("メインハンド y 軸 (左右直結ペア)");
    expect(result.current.state.step_index).toBe(1);
    expect(result.current.state.total_steps).toBe(3);
  });

  it("start は robot を載せない (両ハンド統合の 1 本)", () => {
    const send = vi.fn();
    const { result } = mount({ send });

    result.current.start();
    expect(send).toHaveBeenCalledWith({ type: "motor_check_start" });
  });

  it("abort も robot を載せない", () => {
    const send = vi.fn();
    const { result } = mount({ send });

    result.current.abort();
    expect(send).toHaveBeenCalledWith({ type: "motor_check_abort" });
  });

  it("再レンダーしても start/abort の参照が変わらない", () => {
    const { result, rerender } = mount();
    const before = result.current;

    rerender();
    expect(result.current.start).toBe(before.start);
    expect(result.current.abort).toBe(before.abort);
  });
});
