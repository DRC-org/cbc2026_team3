import { AdviceList } from "@/components/tuning/AdviceList";
import { MetricsPanel } from "@/components/tuning/MetricsPanel";
import { ResponseChart } from "@/components/tuning/ResponseChart";
import { Panel } from "@/components/ui/Panel";
import { MALFORMED, readableMetrics } from "@/lib/protocol";
import type { TuningCapture } from "@/lib/protocol";

/**
 * 直近のステップ応答。**この画面が「感覚」でなくなるかどうかはここに掛かっている。**
 *
 * 記録は操縦者が手動ジョグで動かした 1 回、あるいはシーケンスの移動 1 回から
 * 自動的に取れる。**記録のために機体を動かすボタンは無い** — 機体が動く条件を
 * 増やさないため、既にある操作の結果をそのまま測る形にしてある。
 */
export function ResponsePanel({ motor, captures }: { motor: string; captures: TuningCapture[] }) {
  const [latest, previous] = captures;
  const metrics = latest ? readableMetrics(latest.metrics) : null;

  if (!latest) {
    return (
      <Panel legend="ステップ応答">
        <div className="flex min-h-0 flex-col gap-1 text-base-content/70">
          <p>まだ記録がありません。</p>
          <p className="text-[0.9em] text-base-content/60">
            手動操縦のジョグかシーケンスの移動で {motor} を動かすと、その応答が
            自動で記録されて波形・指標・助言がここに出ます (記録のために機体を動かす
            ボタンはありません)。試合中は配信されません。
          </p>
        </div>
      </Panel>
    );
  }

  return (
    <Panel
      legend="ステップ応答"
      actions={
        <span className="font-mono text-[0.85em] text-base-content/60 tabular-nums">
          kp {latest.gains.kp} / ki {latest.gains.ki} / kd {latest.gains.kd}
        </span>
      }
      bodyClassName="scroll gap-3"
    >
      <ResponseChart capture={latest} previous={previous} />
      {/* 指標が出せない理由は 2 つあり、操縦者の次の一手が違う。**まとめてはならない** —
          「ステップとして解釈できなかった」は動かし方の問題 (もう一度大きく動かす)、
          「配信を読めていない」はサーバー側の問題で、波形の数字も信用できない */}
      {latest.metrics === MALFORMED ? (
        <p className="text-warning">
          指標の配信を読めていません。波形だけ表示しています (サーバーのログを確認してください)。
        </p>
      ) : null}
      {metrics ? (
        <div className="grid gap-3 lg:grid-cols-[minmax(16rem,22rem)_minmax(0,1fr)]">
          <div className="self-start">
            <MetricsPanel
              metrics={metrics}
              previous={readableMetrics(previous?.metrics ?? null) ?? undefined}
            />
          </div>
          <div className="self-start">
            <AdviceList advice={latest.advice} />
          </div>
        </div>
      ) : (
        <AdviceList advice={latest.advice} />
      )}
    </Panel>
  );
}
