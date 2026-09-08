import { Hand } from "lucide-react";

import { SubsystemStatus } from "@/components/diagnostics/SubsystemStatus";
import { Icon } from "@/components/ui/Icon";
import { Panel } from "@/components/ui/Panel";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { cx } from "@/lib/cx";
import type { TempThresholds } from "@/lib/healthVerdict";
import type { RobotState } from "@/lib/protocol";
import { isSequenceComplete, sequenceKind, sequenceProgress } from "@/lib/sequenceStatus";
import type { SequenceKind } from "@/lib/sequenceStatus";
import type { Tone } from "@/lib/tone";
import { TONE_PROGRESS_CLASS } from "@/lib/tone";

interface RobotStatusRowProps {
  label: string;
  state: RobotState | undefined;
  /** サーバーと繋がっているか。切断中の健全性判定は切れた瞬間の値でしかない */
  connected: boolean;
  /** 温度の色分けに使うしきい値。サーバー由来で、Dashboard から流れてくる */
  tempThresholds?: TempThresholds | null;
}

/**
 * 試合中の Monitor が最初に読むべき「その機体は今どうなっているか」。
 * 判定 (どの状態か) は `sequenceKind` に一本化し、ここは表示だけを持つ。
 */
const ACTIVITY: Record<SequenceKind, { tone: Tone; label: string }> = {
  no_sequence: { tone: "neutral", label: "シーケンス未取得" },
  idle: { tone: "neutral", label: "待機中" },
  waiting_trigger: { tone: "warning", label: "許可待ち" },
  running: { tone: "info", label: "実行中" },
  complete: { tone: "success", label: "完走" },
};

/**
 * Monitor 試合中の 1 機分。
 *
 * かつては 8 モータ × 4 値の表がそのまま並び、両機ぶんで 64 個の数字が画面を埋めて、
 * 肝心の「どちらの機体が止まっていて誰の操作待ちか」が沈んでいた。上から
 * 進行状態 → 進捗 → 現在ステップ、と読む順に積み、数値はその下の `SubsystemStatus` へ
 * 送っている —— 主役を入れ替えたのであって、数値を畳んだのではない。
 *
 * その `SubsystemStatus` は **平常時は畳む**（`defaultOpen` を渡さない）。かつて展開して
 * 置いていたが、警告 3 行 + CAN 3 行 + センサ 4 行で領域を使い切り、開いているのに
 * モータが 1 基も見えない状態だった。異常時は `SubsystemStatus` 自身が操縦者の開閉操作を
 * 上書きして開くので、畳んだまま見逃す経路は無い。
 */
export function RobotStatusRow({
  label,
  state,
  connected,
  tempThresholds = null,
}: RobotStatusRowProps) {
  if (!state) {
    return (
      <div className="card flex shrink-0 items-center gap-3 border-base-300 bg-base-100 p-2 card-border">
        <span className="text-[1.2em] font-semibold">{label}</span>
        <StatusBadge tone="error">データ未受信</StatusBadge>
      </div>
    );
  }

  // 手動中はシーケンスが止まっているので、進行状態だけを見ると「待機中」に見える。
  // Monitor から「どちらのハンドが手動か」が分からないと、機体が動いている理由も
  // シーケンスが進まない理由も画面から説明できない
  const inManual = state.manual?.mode === "manual";
  const activity = ACTIVITY[sequenceKind(state)];
  const { waiting_trigger: waiting } = state;
  const isComplete = isSequenceComplete(state);
  // 進捗の算術は `lib/sequenceStatus.ts` の 1 箇所だけが持つ。ここへ写すと、
  // 同じ瞬間に操縦者の画面と Monitor が違う進捗を出す経路が戻る
  const { displayIndex, total, percent, current } = sequenceProgress(state);

  return (
    <Panel accentTone={activity.tone} bodyClassName="p-0">
      <div className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-1 px-2 py-1.5">
        <span className="text-[1.2em] font-semibold">{label}</span>
        <StatusBadge tone={activity.tone}>{activity.label}</StatusBadge>
        {inManual ? <StatusBadge tone="warning">手動操縦中</StatusBadge> : null}
        <span className="ml-auto shrink-0 font-mono text-base-content/70 tabular-nums">
          {displayIndex}
          <span className="text-base-content/45">/{total}</span>
        </span>
      </div>

      <progress
        className={cx(
          "progress h-[0.3rem] w-full shrink-0 rounded-none bg-base-200",
          TONE_PROGRESS_CLASS[activity.tone],
        )}
        value={percent}
        max={100}
      />

      {/* 現在ステップ。Monitor は操作しないので、読めれば十分な大きさに留める */}
      <div className="flex min-w-0 shrink-0 items-center gap-2 px-2 py-1.5 text-[1.15em]">
        {waiting ? <Icon as={Hand} className="shrink-0 text-warning" /> : null}
        <span className="min-w-0 truncate">
          {isComplete ? "全ステップ完了" : (current?.label ?? "—")}
        </span>
      </div>

      <div className="flex min-h-0 flex-1 flex-col border-t border-base-300 px-1 py-1">
        <SubsystemStatus
          health={state.health}
          motors={state.motors}
          safety={state.safety}
          sensors={state.sensors}
          connected={connected}
          tempThresholds={tempThresholds}
        />
      </div>
    </Panel>
  );
}
