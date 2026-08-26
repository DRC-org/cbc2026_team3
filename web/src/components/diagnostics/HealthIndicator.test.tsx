import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { HealthIndicator, formatAge } from "@/components/diagnostics/HealthIndicator";
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
      },
    ],
    motors: [],
    ...over,
  };
}

describe("formatAge", () => {
  it("未取得 (null / undefined / NaN) はダッシュで表す", () => {
    expect(formatAge(null)).toBe("—");
    expect(formatAge(undefined)).toBe("—");
    expect(formatAge(Number.NaN)).toBe("—");
  });

  it("負値は時刻ずれとみなしダッシュで表す", () => {
    expect(formatAge(-1)).toBe("—");
  });

  it("1 秒未満はミリ秒で丸めて出す", () => {
    expect(formatAge(0)).toBe("0ms 前");
    expect(formatAge(123.4)).toBe("123ms 前");
    expect(formatAge(999)).toBe("999ms 前");
  });

  it("1 分未満は小数 1 桁の秒で出す", () => {
    expect(formatAge(1000)).toBe("1.0s 前");
    expect(formatAge(59_999)).toBe("60.0s 前");
  });

  it("1 時間未満は分、それ以上は時間で出す", () => {
    expect(formatAge(60_000)).toBe("1m 前");
    expect(formatAge(3_599_999)).toBe("59m 前");
    expect(formatAge(3_600_000)).toBe("1h 前");
    expect(formatAge(7_200_000)).toBe("2h 前");
  });
});

/**
 * 表示は 1 通りだけ。以前は pill / card / compact / bus-only の 4 variant を持ち、
 * 本番から呼ばれるのは bus-only だけで、残る 3 つはテストからしか到達しなかった。
 * 「使われていないのに緑のまま残るコード」は、読む人に選択肢があると誤解させる。
 */
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
});
