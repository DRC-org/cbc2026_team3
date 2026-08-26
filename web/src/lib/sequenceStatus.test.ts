import { describe, expect, it } from "vitest";

import type { RobotState } from "@/hooks/useRobotSocket";
import { isSequenceComplete, sequenceKind } from "@/lib/sequenceStatus";

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
