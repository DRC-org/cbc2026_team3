import type { ManualAxis, ManualState, RobotState } from "@/lib/protocol";

/**
 * 差分で届いた `state` を前回値へ重ねる。
 *
 * サーバーは細い WiFi を詰まらせないために「前回と同じ欄」を落として配信する
 * (`lib/ws_state.py`)。**欠けた欄は「変わっていない」であって「無い」ではない**ので、
 * ここで前回値を残す。
 */
export function mergeRobotState(previous: RobotState, patch: RobotState): RobotState {
  const merged: RobotState = { ...previous, ...patch };
  if (patch.manual !== undefined && previous.manual !== undefined) {
    merged.manual = mergeManual(previous.manual, patch.manual);
  }
  return merged;
}

/** 手動軸は軸ごとに変わった欄だけ届く。名前で突き合わせ、並びは前回のまま保つ。 */
function mergeManual(previous: ManualState, patch: ManualState): ManualState {
  if (!Array.isArray(previous.axes) || !Array.isArray(patch.axes)) return patch;

  const updates = new Map<string, Partial<ManualAxis>>(
    patch.axes.map((axis) => [axis.name, axis as Partial<ManualAxis>]),
  );
  const axes = previous.axes.map((axis) => {
    const update = updates.get(axis.name);
    updates.delete(axis.name);
    return update === undefined ? axis : { ...axis, ...update };
  });

  return {
    mode: patch.mode ?? previous.mode,
    axes: [...axes, ...(updates.values() as Iterable<ManualAxis>)],
  };
}
