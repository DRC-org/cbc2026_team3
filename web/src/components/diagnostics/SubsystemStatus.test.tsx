import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { SubsystemStatus } from "@/components/diagnostics/SubsystemStatus";
import type { HealthSnapshot, MotorState, SafetyState } from "@/lib/protocol";
import { renderWithRobot } from "@/test/robotContext";

const HEALTH: HealthSnapshot = {
  timestamp: 0,
  overall: "ok",
  buses: [
    {
      name: "can_m3508",
      channel: "can0",
      state: "ok",
      last_tx_at: null,
      last_rx_at: null,
      tx_error_count: 0,
      rx_error_count: 0,
      bus_off: false,
    },
  ],
  motors: [],
};

const MOTORS: Record<string, MotorState> = { y_axis_r: { pos: 0, vel: 0, torque: 0, temp: 30 } };

function safety(over: Partial<SafetyState> = {}): SafetyState {
  return {
    sync_violations: [],
    loops_running: true,
    monitors_running: true,
    position_loops: [{ bus: "can_m3508", running: true, paused: false, sync_violations: [] }],
    sync_monitors: [{ axes: ["y_axis"], running: true, violated: [] }],
    ...over,
  };
}

describe("SubsystemStatus", () => {
  it("平常時は 1 行に畳み、安全機構の行を足さない", () => {
    // 試合中の操縦者が画面へ視線を戻すのは一瞬しかない。平常時は静かに保つ
    renderWithRobot(<SubsystemStatus health={HEALTH} motors={MOTORS} safety={safety()} />);

    expect(screen.getByText("異常なし")).toBeInTheDocument();
    expect(screen.getByRole("button", { expanded: false })).toBeInTheDocument();
    expect(screen.queryByText(/同期ずれ/)).not.toBeInTheDocument();
  });

  it("同期ずれラッチは畳んだ状態を上書きして開き、復旧手順まで出す", () => {
    // 緊急停止を解除してもその軸は動かない。解除操作だけを繰り返させてはならない
    renderWithRobot(
      <SubsystemStatus
        health={HEALTH}
        motors={MOTORS}
        safety={safety({ sync_violations: ["y_axis"] })}
      />,
    );

    expect(screen.getByRole("button", { expanded: true })).toBeInTheDocument();
    expect(screen.getByText("同期ずれラッチ")).toBeInTheDocument();
    expect(screen.getByText("y_axis")).toBeInTheDocument();
    expect(screen.getByText(/解除し直して/)).toBeInTheDocument();
  });

  it("保護ループの停止を自分から主張する", () => {
    // WS は繋がったままモータ状態も届き続けるので、ここに出さないと誰も気付けない
    renderWithRobot(
      <SubsystemStatus
        health={HEALTH}
        motors={MOTORS}
        safety={safety({
          loops_running: false,
          position_loops: [
            { bus: "can_m3508", running: false, paused: false, sync_violations: [] },
          ],
        })}
      />,
    );

    expect(screen.getByText("位置制御ループ停止")).toBeInTheDocument();
    expect(screen.getByRole("button", { expanded: true })).toBeInTheDocument();
  });

  it("判定を別の要素が担う画面では、判定チップも開閉も持たない", () => {
    // Monitor の準備画面は StartGate が「異常があるか」を最大の要素で答える。
    // 同じ文字列をこの見出しにも出すと、同じ事実が同じ画面に 2 回並ぶ
    renderWithRobot(
      <SubsystemStatus health={HEALTH} motors={MOTORS} safety={safety()} showVerdict={false} />,
    );

    expect(screen.queryByText("異常なし")).not.toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    // 中身 (どのバス・どのモータか) は常に見えている
    expect(screen.getByText("can_m3508")).toBeInTheDocument();
  });
});
