import { useState } from "react";

import { ManualAxisRow } from "@/components/operator/ManualAxisRow";
import { Kbd } from "@/components/ui/Kbd";
import { Panel } from "@/components/ui/Panel";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { useHotkeys } from "@/hooks/useHotkeys";
import type { ManualState } from "@/lib/protocol";

interface ManualPanelProps {
  robotKey: string;
  manual: ManualState;
  /** 操作できない理由。null なら操作できる */
  blockedReason: string | null;
  send: (data: object) => boolean;
}

/** キーボードの割り当て。凡例と実装が別々の場所にあると必ず食い違う */
const KEY_LEGEND: { keys: string[]; label: string }[] = [
  { keys: ["↑", "↓"], label: "軸" },
  { keys: ["←", "→"], label: "ジョグ" },
  { keys: ["[", "]"], label: "量" },
  { keys: ["Home", "End"], label: "端" },
];

/**
 * 手動操縦の操作面。軸を 1 行ずつ並べるだけで、軸の並びも可動範囲も
 * サーバー配信 (`state.manual.axes`) をそのまま描く。
 *
 * **この画面に軸名は書かない。** 機構が変わって軸が増減しても UI 側の変更が
 * 要らない性質は、モータ一覧と同じくここを素通しにすることで成立している。
 *
 * **キーボードの操作対象は連続操作できる軸だけ。** プリセットしか持たない軸
 * (電磁弁・グリッパ・duty 軸) を選択に混ぜると、`←` `→` が何も起こさない行へ
 * 降りられてしまい、キーが効かないのか軸が動かないのかを画面から区別できない。
 */
export function ManualPanel({ robotKey, manual, blockedReason, send }: ManualPanelProps) {
  const [picked, setPicked] = useState<string | null>(null);

  const onJog = (axis: string, delta: number) =>
    send({ type: "manual_jog", robot: robotKey, axis, delta });
  const onSet = (axis: string, value: number) =>
    send({ type: "manual_set", robot: robotKey, axis, value });
  const onMove = (axis: string, position: string) =>
    send({ type: "manual_move", robot: robotKey, axis, position });

  const steerable = manual.axes.filter((axis) => axis.manual !== null);
  // 選択は state から導出する。軸が入れ替わっても「居なくなった軸を選んだまま」に
  // ならず、初期選択のための effect も要らない
  const selected = steerable.some((axis) => axis.name === picked)
    ? picked
    : (steerable[0]?.name ?? null);

  const moveSelection = (direction: 1 | -1) => {
    const index = steerable.findIndex((axis) => axis.name === selected);
    if (index < 0) return;
    const next = Math.min(steerable.length - 1, Math.max(0, index + direction));
    setPicked(steerable[next].name);
  };

  // 選択を動かすだけなら機体は動かないので、操作が塞がれていても通す
  // (緊急停止中に見る軸を変えられないほうが不便で、危険は増えない)
  useHotkeys(
    {
      ArrowUp: () => moveSelection(-1),
      ArrowDown: () => moveSelection(1),
    },
    steerable.length > 1,
  );

  return (
    <Panel
      legend="手動操縦"
      className="min-h-0 flex-1"
      bodyClassName="p-0"
      actions={blockedReason ? <StatusBadge tone="error">{blockedReason}</StatusBadge> : null}
    >
      {manual.axes.length === 0 ? (
        <p className="p-2 text-base-content/70">
          このロボットには手動操縦できる軸がありません (位置定数が未読込です)。
        </p>
      ) : (
        <>
          <div className="scroll flex min-h-0 flex-1 flex-col">
            {manual.axes.map((axis) => (
              <ManualAxisRow
                key={axis.name}
                axis={axis}
                blockedReason={blockedReason}
                selected={axis.name === selected}
                onSelect={() => {
                  if (axis.manual !== null) setPicked(axis.name);
                }}
                onJog={onJog}
                onSet={onSet}
                onMove={onMove}
              />
            ))}
          </div>

          {/* 凡例はスクロール領域の外に置く。中に入れると、軸が増えたときに
              一番使う操作の説明だけが画面外へ流れていく */}
          {selected === null ? null : (
            <div className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-1 border-t border-base-300 px-2 py-1 text-[0.8em] text-base-content/55">
              {KEY_LEGEND.map(({ keys, label }) => (
                <span key={label} className="flex shrink-0 items-center gap-1">
                  {keys.map((key) => (
                    <Kbd key={key}>{key}</Kbd>
                  ))}
                  {label}
                </span>
              ))}
              <span className="ml-auto shrink-0">可動範囲内でのみ動きます</span>
            </div>
          )}
        </>
      )}
    </Panel>
  );
}
