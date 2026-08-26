import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Toaster } from "@/components/shell/Toaster";
import { RobotProvider } from "@/context/RobotContext";
import type { CommandRejectedEvent, HealthChangeEvent } from "@/hooks/useRobotSocket";
import { createRobotContext } from "@/test/robotContext";
import type { RobotContextValue } from "@/test/robotContext";

function rejection(over: Partial<CommandRejectedEvent> = {}): CommandRejectedEvent {
  return { command: "match_start", reason: "チェックリスト未完了", receivedAtMs: 1, ...over };
}

function healthEvent(over: Partial<HealthChangeEvent> = {}): HealthChangeEvent {
  return {
    robot: "main_hand",
    level: "warning",
    target: "can0",
    from: "ok",
    to: "degraded",
    message: "受信途絶",
    receivedAtMs: 1,
    ...over,
  };
}

/** コンテキストの差し替えを rerender で行えるようにした描画ヘルパ */
function mount(overrides: Partial<RobotContextValue> = {}) {
  const view = render(
    <RobotProvider value={createRobotContext(overrides)}>
      <Toaster />
    </RobotProvider>,
  );
  const update = (next: Partial<RobotContextValue>) =>
    view.rerender(
      <RobotProvider value={createRobotContext(next)}>
        <Toaster />
      </RobotProvider>,
    );
  return { ...view, update };
}

afterEach(() => {
  vi.useRealTimers();
});

describe("表示するもの", () => {
  it("通知が無ければ何も描画しない", () => {
    const { container } = mount();
    expect(container).toBeEmptyDOMElement();
  });

  it("操作拒否を理由とコマンド名つきで出す", () => {
    mount({ rejection: rejection() });

    expect(screen.getByText(/操作が拒否されました/)).toBeInTheDocument();
    expect(screen.getByText("チェックリスト未完了")).toBeInTheDocument();
    expect(screen.getByText("command: match_start")).toBeInTheDocument();
  });

  it("拒否を受け取ったらコンテキスト側を空へ戻す (同じ操作の連続拒否も再表示するため)", () => {
    const clearRejection = vi.fn();
    mount({ rejection: rejection(), clearRejection });
    expect(clearRejection).toHaveBeenCalled();
  });

  it("ヘルス異常をレベルと対象つきで出す", () => {
    mount({ healthEvents: [healthEvent({ level: "critical" })] });

    expect(screen.getByText(/CRITICAL — main_hand/)).toBeInTheDocument();
    expect(screen.getByText("can0: ok → degraded")).toBeInTheDocument();
    expect(screen.getByText("受信途絶")).toBeInTheDocument();
  });

  it("info レベルのヘルス変化は通知しない (平常運転で画面を埋めないため)", () => {
    const { container } = mount({ healthEvents: [healthEvent({ level: "info" })] });
    expect(container).toBeEmptyDOMElement();
  });

  it("最新のヘルスイベントだけを通知する", () => {
    mount({
      healthEvents: [
        healthEvent({ receivedAtMs: 2, target: "new" }),
        healthEvent({ receivedAtMs: 1, target: "old" }),
      ],
    });

    expect(screen.getByText(/new/)).toBeInTheDocument();
    expect(screen.queryByText(/old/)).not.toBeInTheDocument();
  });
});

describe("寿命と件数の制御", () => {
  it("一定時間で自動的に消える", () => {
    vi.useFakeTimers();
    mount({ rejection: rejection() });
    expect(screen.getByText(/操作が拒否されました/)).toBeInTheDocument();

    act(() => vi.advanceTimersByTime(5000));
    expect(screen.queryByText(/操作が拒否されました/)).not.toBeInTheDocument();
  });

  it("同時表示は 3 件までに絞る (古い通知で直近の異常が埋もれないため)", () => {
    const { update } = mount();

    for (let i = 1; i <= 5; i++) {
      update({ healthEvents: [healthEvent({ receivedAtMs: i, target: `can${i}` })] });
    }

    expect(screen.getAllByText(/WARNING — main_hand/)).toHaveLength(3);
    // 残るのは新しい 3 件
    expect(screen.getByText(/can5/)).toBeInTheDocument();
    expect(screen.queryByText(/can2/)).not.toBeInTheDocument();
  });

  it("閉じるボタンで個別に消せる", async () => {
    mount({ rejection: rejection() });

    await userEvent.click(screen.getByRole("button", { name: "通知を閉じる" }));
    expect(screen.queryByText(/操作が拒否されました/)).not.toBeInTheDocument();
  });
});
