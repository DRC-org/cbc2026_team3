import { memo } from "react";

import { cx } from "@/lib/cx";
import type { TuningMetrics } from "@/lib/protocol";

/**
 * ステップ応答の指標。
 *
 * **測れなかった項目は「—」と出す。** 0 で埋めると、行き過ぎが無かった応答と
 * 窓の中で目標へ届かなかった応答が同じ表示になり、次に取るべき行動が正反対になる。
 *
 * memo なのは `ResponseChart` と同じ理由。記録が増えていないのに、テレメトリで
 * 毎秒 40 回描き直されていた。
 *
 * 前回の値を並べるのは、調整が「変える前より良くなったか」を判断する作業だから。
 * 数字が 1 つだけだと、操縦者は前回を記憶に頼って比べることになる。
 */
export const MetricsPanel = memo(function MetricsPanel({
  metrics,
  previous,
}: {
  metrics: TuningMetrics;
  previous?: TuningMetrics;
}) {
  const rows = buildRows(metrics, previous);

  return (
    <div className="overflow-x-auto">
      <table className="table w-full table-xs">
        <thead>
          <tr className="text-[0.85em]">
            <th className="font-normal text-base-content/60">指標</th>
            <th className="text-right font-normal text-base-content/60">今回</th>
            {previous ? (
              <th className="text-right font-normal text-base-content/60">前回</th>
            ) : null}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.label}>
              <td className="text-[0.85em] whitespace-nowrap">
                {row.label}
                {row.hint ? <span className="ms-1 text-base-content/50">{row.hint}</span> : null}
              </td>
              <td
                className={cx(
                  "text-right font-mono text-[0.85em] tabular-nums",
                  row.emphasize && "font-medium",
                )}
              >
                {row.value}
              </td>
              {previous ? (
                <td className="text-right font-mono text-[0.85em] text-base-content/50 tabular-nums">
                  {row.previous}
                </td>
              ) : null}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
});

interface Row {
  label: string;
  hint?: string;
  value: string;
  previous: string;
  /** 行き過ぎ・整定など、調整で直接動かしたい量を太字にする */
  emphasize?: boolean;
}

function buildRows(metrics: TuningMetrics, previous?: TuningMetrics): Row[] {
  const pair = (pick: (m: TuningMetrics) => string): [string, string] => [
    pick(metrics),
    previous ? pick(previous) : "—",
  ];

  const [rise, risePrev] = pair((m) => ms(m.rise_time_s));
  const [overshoot, overshootPrev] = pair((m) => `${m.overshoot_pct.toFixed(0)}%`);
  const [settling, settlingPrev] = pair((m) => ms(m.settling_time_s));
  const [steady, steadyPrev] = pair(
    (m) => `${m.steady_state_error >= 0 ? "+" : ""}${m.steady_state_error.toFixed(2)}`,
  );
  const [osc, oscPrev] = pair((m) =>
    m.oscillation_hz === null ? "—" : `${m.oscillation_hz.toFixed(1)} Hz`,
  );
  const [damping, dampingPrev] = pair((m) =>
    m.damping_ratio === null ? "—" : m.damping_ratio.toFixed(2),
  );
  const [saturation, saturationPrev] = pair((m) => `${(m.saturation_ratio * 100).toFixed(0)}%`);
  const [step, stepPrev] = pair((m) => `${m.step_size >= 0 ? "+" : ""}${m.step_size.toFixed(2)}`);

  return [
    { label: "ステップ幅", hint: "deg", value: step, previous: stepPrev },
    { label: "立ち上がり", hint: "10→90%", value: rise, previous: risePrev, emphasize: true },
    { label: "行き過ぎ", value: overshoot, previous: overshootPrev, emphasize: true },
    {
      label: "整定",
      hint: `±${metrics.settle_band.toFixed(2)}`,
      value: settling,
      previous: settlingPrev,
      emphasize: true,
    },
    { label: "定常偏差", hint: "deg", value: steady, previous: steadyPrev },
    { label: "振動", value: osc, previous: oscPrev },
    { label: "減衰比", value: damping, previous: dampingPrev },
    { label: "飽和時間率", value: saturation, previous: saturationPrev },
  ];
}

/**
 * 秒をミリ秒表示にする。**null は「—」** で、0 にしない。
 *
 * 立ち上がり時間の null は「窓の中で目標の 90% に届かなかった」、整定時間の
 * null は「窓の終端まで整定帯へ収まらなかった」を意味する。0ms と表示すると
 * どちらも「一瞬で終わった」という正反対の読み方になる。
 */
function ms(seconds: number | null): string {
  return seconds === null ? "—" : `${(seconds * 1000).toFixed(0)} ms`;
}
