import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { HealthIndicator } from "@/components/diagnostics/HealthIndicator";
import type { HealthSnapshot } from "@/lib/protocol";

function snapshot(over: Partial<HealthSnapshot> = {}): HealthSnapshot {
  return {
    timestamp: 0,
    overall: "ok",
    buses: [
      {
        name: "can0",
        channel: "can0",
        state: "ok",
        last_tx_at: null,
        last_rx_at: null,
        tx_error_count: 0,
        rx_error_count: 0,
        bus_off: false,
        rx_down: false,
        rx_down_episodes: 0,
        may_affect_workpiece: false,
      },
    ],
    motors: [],
    detail: null,
    ...over,
  };
}

describe("HealthIndicator", () => {
  it("ヘルス未取得ならプレースホルダを出す", () => {
    render(<HealthIndicator health={undefined} />);
    expect(screen.getByText("CAN")).toBeInTheDocument();
    expect(screen.getByText(/未取得/)).toBeInTheDocument();
  });

  it("overall=ok を成功表示にする", () => {
    render(<HealthIndicator health={snapshot()} />);
    expect(screen.getAllByText(/OK/).length).toBeGreaterThan(0);
  });

  it("overall=degraded / down を異常として表示する", () => {
    const { unmount } = render(<HealthIndicator health={snapshot({ overall: "degraded" })} />);
    expect(screen.getAllByText(/DEGRADED/).length).toBeGreaterThan(0);
    unmount();

    render(<HealthIndicator health={snapshot({ overall: "down" })} />);
    expect(screen.getAllByText(/DOWN/).length).toBeGreaterThan(0);
  });

  it("バス名とチャネル名を並べて表示する", () => {
    const health = snapshot();
    health.buses[0] = { ...health.buses[0], name: "m3508", channel: "can0" };
    render(<HealthIndicator health={health} />);

    expect(screen.getByText("m3508")).toBeInTheDocument();
    expect(screen.getByText("can0")).toBeInTheDocument();
  });

  /**
   * バスの判定 (OK / DEGRADED / DOWN) だけでは「何が起きたか」が分からず、
   * 復旧手順を選べない。エラー計数は 0 のとき出さないので平常時は無音のまま。
   */
  describe("エラー計数の内訳", () => {
    it("平常時 (全て 0) は内訳を出さない", () => {
      render(<HealthIndicator health={snapshot()} />);
      expect(screen.queryByText(/tx_err|rx_err|bus_off/)).not.toBeInTheDocument();
    });

    it("送信エラーと bus-off を内訳に出す", () => {
      const health = snapshot({ overall: "down" });
      health.buses[0] = {
        ...health.buses[0],
        state: "down",
        tx_error_count: 7,
        bus_off: true,
        rx_down: false,
      };
      render(<HealthIndicator health={health} />);

      expect(screen.getByText(/bus_off/)).toBeInTheDocument();
      expect(screen.getByText(/tx_err 7/)).toBeInTheDocument();
    });

    it("受信フレームの解釈失敗数を内訳に出す", () => {
      const health = snapshot();
      health.buses[0] = { ...health.buses[0], rx_error_count: 12 };
      render(<HealthIndicator health={health} />);

      expect(screen.getByText(/rx_err 12/)).toBeInTheDocument();
    });

    /**
     * 受信の解釈失敗でバスを降格させないのはサーバー側の意図的な判断
     * (lib/can_manager.py `_record_rx_error`)。表示がそれを覆して警告色を出すと、
     * 同じ画面が本物の送信障害の警告と区別できなくなる。
     */
    it("受信エラーがあってもバスの判定は動かさない", () => {
      const health = snapshot();
      health.buses[0] = { ...health.buses[0], rx_error_count: 12 };
      render(<HealthIndicator health={health} />);

      expect(screen.queryByText(/DEGRADED|DOWN/)).not.toBeInTheDocument();
      expect(screen.getAllByText(/OK/).length).toBeGreaterThan(0);
    });
  });
});
