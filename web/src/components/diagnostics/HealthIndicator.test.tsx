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
    detail: null,
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
