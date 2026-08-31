import { render } from "@testing-library/react";
import { memo } from "react";
import { describe, expect, it, vi } from "vitest";

import { ResponsePanel } from "@/components/tuning/ResponsePanel";
import type { TuningCapture } from "@/lib/protocol";

/**
 * `/pid-tuning` はテレメトリ (20Hz × 2 台) を購読するので、記録が 1 件も増えて
 * いなくても毎秒 40 回再描画される。波形は最大 300 点 × 3 本の SVG path 文字列を
 * 組み立てるため、素通しにすると調整中ずっと無駄な生成が走り続ける。
 *
 * **memo だけでも props の安定だけでも足りない。** 親が再描画すると React は memo の
 * 無い子を素通しで描き直し、逆に memo があっても親が毎描画 新しいオブジェクトを
 * 渡せば効かない。ここでは 2 つを別々に固定する。
 */

const counts = vi.hoisted(() => ({ chart: 0, metrics: 0, advice: 0 }));

/**
 * 差し替えも memo にしておく。こうすると、この計数テストを落とせるのは
 * **親が毎描画 新しい props を渡す場合だけ**になる (本物に memo が付いているかは
 * 下の別テストが見る)。
 */
vi.mock("@/components/tuning/ResponseChart", () => ({
  ResponseChart: memo(function ResponseChart() {
    counts.chart += 1;
    return null;
  }),
}));

vi.mock("@/components/tuning/MetricsPanel", () => ({
  MetricsPanel: memo(function MetricsPanel() {
    counts.metrics += 1;
    return null;
  }),
}));

vi.mock("@/components/tuning/AdviceList", () => ({
  AdviceList: memo(function AdviceList() {
    counts.advice += 1;
    return null;
  }),
}));

const CAPTURE: TuningCapture = {
  robot: "main_hand",
  motor: "lift",
  captured_at: 1700000000,
  gains: { kp: 2, ki: 0, kd: 0 },
  metrics: {
    step_from: 0,
    step_to: 10,
    step_size: 10,
    rise_time_s: 0.05,
    overshoot_pct: 35,
    peak_time_s: 0.08,
    settling_time_s: 0.12,
    steady_state_error: 0,
    oscillation_hz: null,
    damping_ratio: null,
    saturation_ratio: 0.375,
    peak_output: 900,
    settle_band: 1,
    sample_count: 4,
    duration_s: 0.14,
  },
  advice: [{ code: "overshoot", severity: "info", message: "行き過ぎが 35% あります。" }],
  samples: {
    t: [0, 0.02, 0.04, 0.06],
    target: [10, 10, 10, 10],
    pos: [0, 5, 13.5, 10],
    output: [900, 700, 300, 100],
    sat: [true, false, false, false],
  },
};

/** React が memo でラップした値か。memo は出力に現れないので DOM からは観測できない */
function isMemo(component: unknown): boolean {
  return (component as { $$typeof?: symbol }).$$typeof === Symbol.for("react.memo");
}

describe("ステップ応答の再描画", () => {
  it("記録が増えていなければ波形も指標も助言も描き直さない", () => {
    counts.chart = 0;
    counts.metrics = 0;
    counts.advice = 0;

    // 記録の配列は同じ参照のまま。テレメトリ由来の再描画をこれで模す
    const captures = [CAPTURE];
    const { rerender } = render(<ResponsePanel motor="lift" captures={captures} />);

    expect(counts.chart).toBe(1);
    for (let i = 0; i < 20; i++) {
      rerender(<ResponsePanel motor="lift" captures={captures} />);
    }

    expect(counts.chart).toBe(1);
    expect(counts.metrics).toBe(1);
    expect(counts.advice).toBe(1);
  });

  it("記録が増えたら描き直す (止まっていたら値が凍る)", () => {
    counts.chart = 0;
    const { rerender } = render(<ResponsePanel motor="lift" captures={[CAPTURE]} />);
    rerender(
      <ResponsePanel motor="lift" captures={[{ ...CAPTURE, captured_at: 1700000001 }, CAPTURE]} />,
    );

    expect(counts.chart).toBe(2);
  });

  it("波形・指標・助言は memo で包まれている", async () => {
    // 上の計数テストは差し替えを見ているので、本物から memo が外れても落ちない。
    // memo の有無は出力に現れない (同じ DOM が出る) ため、ここだけは型で見る
    const [chart, metrics, advice] = await Promise.all([
      vi.importActual<typeof import("@/components/tuning/ResponseChart")>(
        "@/components/tuning/ResponseChart",
      ),
      vi.importActual<typeof import("@/components/tuning/MetricsPanel")>(
        "@/components/tuning/MetricsPanel",
      ),
      vi.importActual<typeof import("@/components/tuning/AdviceList")>(
        "@/components/tuning/AdviceList",
      ),
    ]);

    expect(isMemo(chart.ResponseChart)).toBe(true);
    expect(isMemo(metrics.MetricsPanel)).toBe(true);
    expect(isMemo(advice.AdviceList)).toBe(true);
  });
});
