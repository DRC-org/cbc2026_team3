import { useId } from "react";

import { readableMetrics } from "@/lib/protocol";
import type { TuningCapture } from "@/lib/protocol";

/**
 * ステップ応答の波形。
 *
 * **瞬時値の羅列からは行き過ぎも振動も原理的に読めない。** 以前この画面が出して
 * いたのは POS / VEL / TORQUE / TEMP の 4 つの数字だけで、ピークの高さも整定の
 * 有無も操縦者の記憶に頼るしかなかった。ここが「感覚で操作するしかない」の本体。
 *
 * 描画は素の SVG で行う。会場のネットワークに依存させないため外部ライブラリを
 * 足さない方針で、この用途 (折れ線 2〜3 本と帯) なら自前で足りる。
 *
 * 色は daisyUI のセマンティックカラーを `currentColor` 経由で受ける。SVG 属性へ
 * 直接カラーコードを書くと、ライト / ダークの切り替えから外れる。
 */
export function ResponseChart({
  capture,
  previous,
}: {
  capture: TuningCapture;
  /** 直前の記録。ゲインを変える前の応答を薄く重ねる (比較が調整作業そのもの) */
  previous?: TuningCapture;
}) {
  const clipId = useId();
  const { samples } = capture;
  // 読めない指標から整定帯を描かない (画面に「測ったように見える帯」が出る)
  const metrics = readableMetrics(capture.metrics);
  if (samples.t.length < 2) {
    return <p className="text-base-content/60">波形を描くにはサンプルが足りません。</p>;
  }

  const view = layout(capture, previous);

  return (
    <div className="overflow-x-auto">
      <svg
        viewBox={`0 0 ${VIEW_W} ${VIEW_H}`}
        className="h-48 w-full min-w-[20rem]"
        role="img"
        aria-label="ステップ応答の波形"
        preserveAspectRatio="none"
      >
        <title>ステップ応答の波形</title>
        <clipPath id={clipId}>
          <rect x={PAD_L} y={PAD_T} width={PLOT_W} height={PLOT_H} />
        </clipPath>

        {/* 整定帯。ここへ収まって以降ずっと留まった時刻が整定時間 */}
        {metrics ? (
          <rect
            x={PAD_L}
            y={view.y(metrics.step_to + metrics.settle_band)}
            width={PLOT_W}
            height={Math.max(
              1,
              view.y(metrics.step_to - metrics.settle_band) -
                view.y(metrics.step_to + metrics.settle_band),
            )}
            className="fill-success/15"
          />
        ) : null}

        {/* 飽和していた区間。ゲインを変えても応答が変わらない時間帯を地色で示す */}
        <g clipPath={`url(#${clipId})`} className="fill-warning/20">
          {saturatedBands(capture, view).map((band) => (
            <rect
              key={`sat-${band.from}`}
              x={band.from}
              y={PAD_T}
              width={Math.max(1, band.to - band.from)}
              height={PLOT_H}
            />
          ))}
        </g>

        {/* ステップの瞬間 (t=0)。これより左は記録した直前の様子 */}
        <line
          x1={view.x(0)}
          x2={view.x(0)}
          y1={PAD_T}
          y2={PAD_T + PLOT_H}
          className="stroke-base-content/25"
          strokeWidth={1}
          strokeDasharray="2 2"
        />

        <g clipPath={`url(#${clipId})`} fill="none">
          {previous ? (
            <path
              d={linePath(previous.samples.t, previous.samples.pos, view)}
              className="stroke-base-content/25"
              strokeWidth={1.5}
            />
          ) : null}
          <path
            d={linePath(samples.t, samples.target, view)}
            className="stroke-base-content/50"
            strokeWidth={1.5}
            strokeDasharray="4 3"
          />
          <path
            d={linePath(samples.t, samples.pos, view)}
            className="stroke-info"
            strokeWidth={2}
          />
        </g>

        <rect
          x={PAD_L}
          y={PAD_T}
          width={PLOT_W}
          height={PLOT_H}
          fill="none"
          className="stroke-base-300"
          strokeWidth={1}
        />
      </svg>

      <div className="mt-1 flex flex-wrap items-center gap-x-4 gap-y-1 text-[0.8em] text-base-content/70">
        <Legend className="bg-info">実測</Legend>
        <Legend className="bg-base-content/50">目標</Legend>
        {previous ? <Legend className="bg-base-content/25">前回</Legend> : null}
        <Legend className="bg-success/40">整定帯</Legend>
        <Legend className="bg-warning/40">飽和</Legend>
        <span className="ms-auto tabular-nums">
          {view.tMin.toFixed(2)}s 〜 {view.tMax.toFixed(2)}s
        </span>
      </div>
    </div>
  );
}

function Legend({ className, children }: { className: string; children: string }) {
  return (
    <span className="flex items-center gap-1">
      <span className={`inline-block h-2 w-3 ${className}`} />
      {children}
    </span>
  );
}

// viewBox の座標系。preserveAspectRatio="none" で横に伸ばすので、
// ここでの数値は実寸ではなく比率としてだけ意味を持つ
const VIEW_W = 600;
const VIEW_H = 200;
const PAD_L = 4;
const PAD_T = 4;
const PLOT_W = VIEW_W - PAD_L * 2;
const PLOT_H = VIEW_H - PAD_T * 2;

interface View {
  x: (t: number) => number;
  y: (value: number) => number;
  tMin: number;
  tMax: number;
}

/**
 * 縦横のスケールを決める。**前回の記録も含めて範囲を取る。**
 *
 * 今回ぶんだけで範囲を決めると、2 本の線が別々の縮尺で重なり、変化していない
 * 応答が変わったように見える (比較のために重ねているのに、比較が壊れる)。
 */
function layout(capture: TuningCapture, previous?: TuningCapture): View {
  const times = [...capture.samples.t, ...(previous?.samples.t ?? [])];
  const values = [
    ...capture.samples.pos,
    ...capture.samples.target,
    ...(previous?.samples.pos ?? []),
  ];
  const metrics = readableMetrics(capture.metrics);
  if (metrics) {
    values.push(metrics.step_to + metrics.settle_band, metrics.step_to - metrics.settle_band);
  }

  const tMin = Math.min(...times);
  const tMax = Math.max(...times);
  const vMin = Math.min(...values);
  const vMax = Math.max(...values);
  // 縦横とも 0 幅を避ける。0 除算は NaN の座標になり、線が 1 本も描かれない
  const tSpan = tMax - tMin || 1;
  // 上下に 8% の余白。ピークが枠線と重なると行き過ぎの量が読み取れない
  const margin = (vMax - vMin || 1) * 0.08;
  const vLo = vMin - margin;
  const vSpan = vMax + margin - vLo || 1;

  return {
    x: (t) => PAD_L + ((t - tMin) / tSpan) * PLOT_W,
    y: (value) => PAD_T + PLOT_H - ((value - vLo) / vSpan) * PLOT_H,
    tMin,
    tMax,
  };
}

function linePath(times: number[], values: number[], view: View): string {
  return times
    .map(
      (t, index) =>
        `${index === 0 ? "M" : "L"}${view.x(t).toFixed(2)} ${view.y(values[index]).toFixed(2)}`,
    )
    .join(" ");
}

/** 飽和していた連続区間を x 座標の帯へ畳む。1 点ごとに矩形を出すと要素数が点数ぶん増える */
function saturatedBands(capture: TuningCapture, view: View): { from: number; to: number }[] {
  const { t, sat } = capture.samples;
  const bands: { from: number; to: number }[] = [];
  let start: number | null = null;
  for (let index = 0; index < sat.length; index += 1) {
    if (sat[index] && start === null) start = index;
    if (!sat[index] && start !== null) {
      bands.push({ from: view.x(t[start]), to: view.x(t[index]) });
      start = null;
    }
  }
  if (start !== null) bands.push({ from: view.x(t[start]), to: view.x(t[t.length - 1]) });
  return bands;
}
