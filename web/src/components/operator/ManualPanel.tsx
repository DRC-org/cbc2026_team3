import { ManualAxisRow } from "@/components/operator/ManualAxisRow";
import { Panel } from "@/components/ui/Panel";
import { StatusBadge } from "@/components/ui/StatusBadge";
import type { ManualState } from "@/lib/protocol";

interface ManualPanelProps {
  robotKey: string;
  manual: ManualState;
  /** 操作できない理由。null なら操作できる */
  blockedReason: string | null;
  send: (data: object) => boolean;
}

/**
 * 手動操縦の操作面。軸を 1 行ずつ並べるだけで、軸の並びも可動範囲も
 * サーバー配信 (`state.manual.axes`) をそのまま描く。
 *
 * **この画面に軸名は書かない。** 機構が変わって軸が増減しても UI 側の変更が
 * 要らない性質は、モータ一覧と同じくここを素通しにすることで成立している。
 */
export function ManualPanel({ robotKey, manual, blockedReason, send }: ManualPanelProps) {
  const onJog = (axis: string, delta: number) =>
    send({ type: "manual_jog", robot: robotKey, axis, delta });
  const onSet = (axis: string, value: number) =>
    send({ type: "manual_set", robot: robotKey, axis, value });
  const onMove = (axis: string, position: string) =>
    send({ type: "manual_move", robot: robotKey, axis, position });

  return (
    <Panel
      legend="手動操縦"
      className="min-h-0 flex-1"
      bodyClassName="p-0"
      actions={
        blockedReason ? (
          <StatusBadge tone="error">{blockedReason}</StatusBadge>
        ) : (
          <span className="text-[0.85em] text-base-content/60">可動範囲内でのみ動きます</span>
        )
      }
    >
      {manual.axes.length === 0 ? (
        <p className="p-2 text-base-content/70">
          このロボットには手動操縦できる軸がありません (位置定数が未読込です)。
        </p>
      ) : (
        <div className="scroll flex min-h-0 flex-1 flex-col">
          {manual.axes.map((axis) => (
            <ManualAxisRow
              key={axis.name}
              axis={axis}
              blockedReason={blockedReason}
              onJog={onJog}
              onSet={onSet}
              onMove={onMove}
            />
          ))}
        </div>
      )}
    </Panel>
  );
}
