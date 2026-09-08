import { describe, expect, it } from "vitest";

import { parseServerMessage } from "@/lib/protocol";
import type { RobotUiState } from "@/lib/robotReducer";
import { INITIAL_ROBOT_UI_STATE, robotReducer } from "@/lib/robotReducer";

const NOW = 1_700_000_000_000;

function receive(state: RobotUiState, payload: object, nowMs = NOW): RobotUiState {
  const message = parseServerMessage(JSON.stringify(payload));
  if (!message) throw new Error(`受信条件に弾かれました: ${JSON.stringify(payload)}`);
  return robotReducer(state, { type: "message", message, nowMs });
}

describe("robotReducer", () => {
  it("入力の state を書き換えない (純関数)", () => {
    const before = INITIAL_ROBOT_UI_STATE;
    const snapshot = structuredClone(before);
    receive(before, { type: "state", robot: "main_hand", step_index: 1 });
    expect(before).toEqual(snapshot);
  });

  it("state 受信で他領域の参照を作り直さない", () => {
    const first = receive(INITIAL_ROBOT_UI_STATE, {
      type: "match_state",
      court: "red",
      phase: "match",
    });
    const next = receive(first, { type: "state", robot: "main_hand", step_index: 2 });

    expect(next.states).not.toBe(first.states);
    expect(next.matchState).toBe(first.matchState);
    expect(next.motorCheck).toBe(first.motorCheck);
    expect(next.healthEvents).toBe(first.healthEvents);
    expect(next.rejection).toBe(first.rejection);
  });

  it("動作確認の受信で states の参照を作り直さない", () => {
    const first = receive(INITIAL_ROBOT_UI_STATE, {
      type: "state",
      robot: "main_hand",
      step_index: 1,
    });
    const next = receive(first, {
      type: "motor_check_state",
      available: true,
      running: true,
      step_index: 1,
      total_steps: 4,
    });

    expect(next.states).toBe(first.states);
    expect(next.motorCheck.running).toBe(true);
  });

  it("state の e_stop_active 解除で理由を畳む", () => {
    const stopped = receive(INITIAL_ROBOT_UI_STATE, {
      type: "e_stop_state",
      active: true,
      reason: "同期ずれ y_axis",
    });
    expect(stopped.eStopReason).toBe("同期ずれ y_axis");

    const during = receive(stopped, { type: "state", robot: "main_hand", e_stop_active: true });
    expect(during.eStopReason).toBe("同期ずれ y_axis");

    const released = receive(during, { type: "state", robot: "main_hand", e_stop_active: false });
    expect(released.eStopActive).toBe(false);
    expect(released.eStopReason).toBeNull();
  });

  it("操縦者操作による緊急停止の楽観的更新を受ける", () => {
    const active = robotReducer(INITIAL_ROBOT_UI_STATE, { type: "e_stop_local", active: true });
    expect(active.eStopActive).toBe(true);

    const released = robotReducer(active, { type: "e_stop_local", active: false });
    expect(released.eStopActive).toBe(false);
  });

  it("拒否は受信時刻付きで保持し、clear_rejection で消える", () => {
    const rejected = receive(INITIAL_ROBOT_UI_STATE, {
      type: "command_rejected",
      command: "sequence_start",
      reason: "手動操縦中はシーケンスを開始できません",
    });
    expect(rejected.rejection).toEqual({
      command: "sequence_start",
      reason: "手動操縦中はシーケンスを開始できません",
      receivedAtMs: NOW,
      source: "server",
    });

    expect(robotReducer(rejected, { type: "clear_rejection" }).rejection).toBeNull();
  });

  it("送信できなかった操作を、サーバーの拒否と区別して保持する", () => {
    const unsent = robotReducer(INITIAL_ROBOT_UI_STATE, {
      type: "command_unsent",
      command: "e_stop",
      reason: "切断中のため緊急停止を送信できませんでした",
      nowMs: NOW,
    });

    expect(unsent.rejection).toEqual({
      command: "e_stop",
      reason: "切断中のため緊急停止を送信できませんでした",
      receivedAtMs: NOW,
      source: "local",
    });
  });

  it("動作確認は継ぎ足さず、届いた状態でまるごと置き換える", () => {
    const running = receive(INITIAL_ROBOT_UI_STATE, {
      type: "motor_check_state",
      available: true,
      running: true,
      current_step: "メインハンド y 軸",
      step_index: 1,
      total_steps: 3,
    });
    expect(running.motorCheck.current_step).toBe("メインハンド y 軸");

    const finished = receive(running, {
      type: "motor_check_state",
      available: true,
      running: false,
      step_index: 3,
      total_steps: 3,
    });

    expect(finished.motorCheck.running).toBe(false);
    expect(finished.motorCheck.current_step).toBeNull();
  });

  it("ヘルス変化は新しい順に 5 件だけ残す", () => {
    let state = INITIAL_ROBOT_UI_STATE;
    for (let i = 0; i < 8; i++) {
      state = receive(state, { type: "health_change", robot: "main_hand", target: `m${i}` });
    }
    expect(state.healthEvents.map((e) => e.target)).toEqual(["m7", "m6", "m5", "m4", "m3"]);
  });

  describe("同値の再配信", () => {
    it("同じ e_stop_state を受けても参照を作り直さない", () => {
      const first = receive(INITIAL_ROBOT_UI_STATE, { type: "e_stop_state", active: true });
      const second = receive(first, { type: "e_stop_state", active: true });

      expect(second).toBe(first);
    });

    it("理由が変われば取り込む (最初の理由を優先する仕組みはサーバー側)", () => {
      const first = receive(INITIAL_ROBOT_UI_STATE, { type: "e_stop_state", active: true });
      const second = receive(first, {
        type: "e_stop_state",
        active: true,
        reason: "同期ずれを検知しました (y_axis)",
      });

      expect(second).not.toBe(first);
      expect(second.eStopReason).toBe("同期ずれを検知しました (y_axis)");
    });

    it("解除は当然取り込む", () => {
      const first = receive(INITIAL_ROBOT_UI_STATE, { type: "e_stop_state", active: true });
      const second = receive(first, { type: "e_stop_state", active: false });

      expect(second.eStopActive).toBe(false);
    });
  });
});
