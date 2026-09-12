import { ManualAxisRow } from "@/components/operator/ManualAxisRow";
import { OnOffPadGroup, splitOnOffAxes } from "@/components/operator/OnOffPadGroup";
import { Panel } from "@/components/ui/Panel";
import { StatusBadge } from "@/components/ui/StatusBadge";
import type { RobotCommands } from "@/context/RobotContext";
import type { ManualState } from "@/lib/protocol";

interface AlwaysManualPanelProps {
  robotKey: string;
  manual: ManualState;
  blockedReason: string | null;
  sendOrReport: RobotCommands["sendOrReport"];
  /** 別のパネルが持つ軸。吸着パッドの弁は吸着パッドの面が「今すぐ開閉」を持つ */
  excludeAxes?: string[];
}

export function AlwaysManualPanel({
  robotKey,
  manual,
  blockedReason,
  sendOrReport,
  excludeAxes,
}: AlwaysManualPanelProps) {
  // 欄が落ちた配信でパネルごと消える側へ倒すため厳密に比較する。シーケンス実行中に
  // 押せるボタンが、配信の欠落で増えてはならない
  const axes = manual.axes.filter(
    (axis) => axis.manual_always === true && !(excludeAxes ?? []).includes(axis.name),
  );
  if (axes.length === 0) return null;
  const { pads, rest } = splitOnOffAxes(axes);

  const onMove = (axis: string, position: string) =>
    sendOrReport({ type: "manual_move", robot: robotKey, axis, position }, "プリセット移動");
  const onJog = (axis: string, delta: number) =>
    sendOrReport({ type: "manual_jog", robot: robotKey, axis, delta }, "ジョグ");
  const onSet = (axis: string, value: number) =>
    sendOrReport({ type: "manual_set", robot: robotKey, axis, value }, "目標値の送信");

  return (
    <Panel
      legend="常時操作"
      className="shrink-0"
      bodyClassName="@container p-0"
      actions={blockedReason ? <StatusBadge tone="error">{blockedReason}</StatusBadge> : null}
    >
      <OnOffPadGroup axes={pads} blockedReason={blockedReason} onMove={onMove} />

      <div className="grid @min-[40rem]:grid-cols-2 @min-[56rem]:grid-cols-3">
        {rest.map((axis) => (
          <ManualAxisRow
            key={axis.name}
            axis={axis}
            blockedReason={blockedReason}
            selected={false}
            onSelect={() => {}}
            onJog={onJog}
            onSet={onSet}
            onMove={onMove}
          />
        ))}
      </div>
    </Panel>
  );
}
