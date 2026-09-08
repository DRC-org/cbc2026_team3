import { MotorStatHeader, MotorStatus } from "@/components/diagnostics/MotorStatus";
import { ScrollArea } from "@/components/ui/ScrollArea";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { summarizeMotors } from "@/lib/healthVerdict";
import type { TempThresholds } from "@/lib/healthVerdict";
import type { MotorHealth, MotorState } from "@/lib/protocol";

interface MotorSummaryProps {
  motors: Record<string, MotorState>;
  healthMotors?: MotorHealth[];
  /** 温度の色分けに使うしきい値。サーバー由来で、SubsystemStatus から流れてくる */
  tempThresholds?: TempThresholds | null;
}

export function MotorSummary({ motors, healthMotors, tempThresholds = null }: MotorSummaryProps) {
  const total = Object.keys(motors).length;
  const healthMap = Object.fromEntries((healthMotors ?? []).map((m) => [m.name, m]));

  if (total === 0) {
    return <div className="text-base-content/70">モータ情報なし</div>;
  }

  // 判定は healthVerdict に一本化してある。ここに条件を書き足さないこと
  const verdict = summarizeMotors(healthMotors);

  // **行の形は「誰が呼んだか」ではなく実際の幅で決める。** 同じ部品が幅の違う
  // 4 つの場所で使われるので、props で切り替えると呼び出し元がレイアウトの都合を
  // 知ることになる。**閾値と実測値は `MotorStatus` の `NAME_COL_CLASS` が持つ**
  // (ここへ書き写すと、同じ画面を指す数字が 2 つできる)。この div がその比較対象
  return (
    <div className="@container flex min-h-0 flex-1 flex-col gap-1">
      <div className="flex shrink-0 items-center justify-between gap-2">
        <span className="text-base-content/70">{total} 基</span>
        <StatusBadge tone={verdict.tone}>{verdict.label}</StatusBadge>
      </div>
      <MotorStatHeader className="shrink-0 border-b border-base-300 pb-[0.1rem]" />
      {/* 縞は交互の地色。等幅の数値が縦に並ぶ表で行を追いやすくする。
          行が 2 段組みで table には収まらないため、変則的な行の縞は変種セレクタで表す */}
      <ScrollArea className="[&>*:nth-child(odd)]:bg-base-200">
        {Object.entries(motors).map(([name, state]) => (
          <MotorStatus
            key={name}
            name={name}
            state={state}
            health={healthMap[name]}
            tempThresholds={tempThresholds}
          />
        ))}
      </ScrollArea>
    </div>
  );
}
