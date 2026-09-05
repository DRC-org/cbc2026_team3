import { describe, expect, it } from "vitest";

import type { RobotState } from "@/lib/protocol";
import { isSequenceComplete, sequenceKind, sequenceProgress } from "@/lib/sequenceStatus";

function state(over: Partial<RobotState> = {}): RobotState {
  return {
    robot: "main_hand",
    sequence: "main_hand",
    current_step: null,
    step_index: 0,
    total_steps: 6,
    waiting_trigger: false,
    running: false,
    steps: [],
    motors: {},
    ...over,
  };
}

/**
 * 実行状態は `running` (サーバー配信) だけで決める。
 * `step_index === 0 && total_steps > 0` を「未実行」の代用にしていた頃は、
 * 準備フェーズで条件が常に成立して動作確認ボタンが常時無効になった。
 */
describe("sequenceKind", () => {
  it("シーケンス未取得", () => {
    expect(sequenceKind(state({ total_steps: 0 }))).toBe("no_sequence");
  });

  it("未開始は idle", () => {
    expect(sequenceKind(state({ running: false, step_index: 0 }))).toBe("idle");
  });

  it("running なら実行中", () => {
    expect(sequenceKind(state({ running: true, step_index: 2 }))).toBe("running");
  });

  it("トリガー待ちは実行中より先に立つ (押すべきボタンが変わるため)", () => {
    expect(sequenceKind(state({ running: true, waiting_trigger: true, step_index: 2 }))).toBe(
      "waiting_trigger",
    );
  });

  it("完走は step_index が総数に達した状態", () => {
    expect(sequenceKind(state({ running: false, step_index: 6 }))).toBe("complete");
  });

  it("途中で停止した状態を実行中と偽らない", () => {
    // 以前は step_index > 0 だけで「実行中」と表示し、STOP 後も RUNNING を出していた
    expect(sequenceKind(state({ running: false, step_index: 3 }))).toBe("idle");
  });

  it("running が未受信 (旧サーバー) でも例外にせず未開始扱いにする", () => {
    expect(sequenceKind(state({ running: undefined, step_index: 0 }))).toBe("idle");
  });
});

describe("isSequenceComplete", () => {
  it("完走時のみ true", () => {
    expect(isSequenceComplete(state({ step_index: 6 }))).toBe(true);
    expect(isSequenceComplete(state({ step_index: 5 }))).toBe(false);
    expect(isSequenceComplete(state({ total_steps: 0 }))).toBe(false);
  });

  it("トリガー待ちなら完走ではない", () => {
    expect(isSequenceComplete(state({ step_index: 6, waiting_trigger: true }))).toBe(false);
  });
});

/**
 * 進捗バーの分子は**完了したステップ数**であって、操縦者に見せる現在ステップ番号
 * (`displayIndex` = `step_index + 1`) ではない。両者で同じ式を使っていた頃は
 * 常に 1 マス先行し、シーケンスを開始していない試合開始直後の画面が
 * 「1/13 だけ進んだバー」を出していた。
 */
describe("sequenceProgress", () => {
  const steps = Array.from({ length: 6 }, (_, i) => ({
    index: i,
    label: `step${i}`,
    require_trigger: false,
  }));

  it("開始していなければ 0%", () => {
    const { percent, displayIndex } = sequenceProgress(state({ steps }));
    expect(percent).toBe(0);
    // バーは 0 でも、これから走るステップの番号は 1 始まりで出す
    expect(displayIndex).toBe(1);
  });

  it("実行中は完了したぶんだけ進む (走行中のステップは数えない)", () => {
    const { percent, displayIndex } = sequenceProgress(
      state({ steps, running: true, step_index: 3 }),
    );
    expect(percent).toBe(50);
    expect(displayIndex).toBe(4);
  });

  it("トリガー待ちは次のステップを済んだことにしない", () => {
    const { percent } = sequenceProgress(
      state({ steps, running: true, waiting_trigger: true, step_index: 3 }),
    );
    expect(percent).toBe(50);
  });

  it("完走は 100% で、総数を超えない", () => {
    const { percent, displayIndex, current } = sequenceProgress(state({ steps, step_index: 6 }));
    expect(percent).toBe(100);
    expect(displayIndex).toBe(6);
    // 完走後は「今いるステップ」が無い
    expect(current).toBeUndefined();
  });

  it("シーケンス未取得なら 0 除算せず 0%", () => {
    expect(sequenceProgress(state({ total_steps: 0, steps: [] }))).toEqual({
      displayIndex: 0,
      total: 0,
      percent: 0,
      current: undefined,
    });
  });

  it("現在ステップはステップ表から引く", () => {
    expect(sequenceProgress(state({ steps, running: true, step_index: 2 })).current).toEqual(
      steps[2],
    );
  });
});
