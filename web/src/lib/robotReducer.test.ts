import { describe, expect, it } from "vitest";

import { parseServerMessage } from "@/lib/protocol";
import type { RobotUiState } from "@/lib/robotReducer";
import { INITIAL_ROBOT_UI_STATE, robotReducer } from "@/lib/robotReducer";

const NOW = 1_700_000_000_000;

/** 受信経路と同じ形 (JSON → parse → reducer) で 1 通流す */
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

  /**
   * 20Hz × 2 台で毎秒 40 回届く state が、試合状態や動作確認の参照まで作り直すと
   * それらを読むだけの画面 (チェックリスト・タブ・トースト) が同じ頻度で再描画される。
   * 触っていない領域は参照ごと据え置く。
   */
  it("state 受信で他領域の参照を作り直さない", () => {
    const first = receive(INITIAL_ROBOT_UI_STATE, {
      type: "match_state",
      court: "red",
      phase: "match",
    });
    const next = receive(first, { type: "state", robot: "main_hand", step_index: 2 });

    expect(next.states).not.toBe(first.states);
    expect(next.matchState).toBe(first.matchState);
    expect(next.motorChecks).toBe(first.motorChecks);
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
      type: "motor_check_progress",
      robot: "main_hand",
      index: 1,
      total: 4,
    });

    expect(next.states).toBe(first.states);
    expect(next.motorChecks.main_hand.status).toBe("running");
  });

  it("state の e_stop_active 解除で理由を畳む", () => {
    const stopped = receive(INITIAL_ROBOT_UI_STATE, {
      type: "e_stop_state",
      active: true,
      reason: "同期ずれ y_axis",
    });
    expect(stopped.eStopReason).toBe("同期ずれ y_axis");

    // 発動中の state 配信では理由を消さない (理由は e_stop_state だけが運ぶ)
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
      command: "set_param",
      reason: "試合中はパラメータを変更できません",
    });
    expect(rejected.rejection).toEqual({
      command: "set_param",
      reason: "試合中はパラメータを変更できません",
      receivedAtMs: NOW,
      source: "server",
    });

    expect(robotReducer(rejected, { type: "clear_rejection" }).rejection).toBeNull();
  });

  it("送信できなかった操作を、サーバーの拒否と区別して保持する", () => {
    // 「サーバーが断った」と「そもそも届いていない」では操縦者の次の一手が違う。
    // 前者は条件を満たせば通るが、後者は機体が指令を受け取っていない
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

  describe("motor_check_*", () => {
    it("progress → record → done を 1 本の実行として畳む", () => {
      const running = receive(INITIAL_ROBOT_UI_STATE, {
        type: "motor_check_progress",
        robot: "main_hand",
        current: "lift",
        index: 0,
        total: 2,
      });
      expect(running.motorChecks.main_hand.startedAtMs).toBe(NOW);

      const withRecord = receive(running, {
        type: "motor_check_record",
        robot: "main_hand",
        record: { motor: "lift", result: "passed" },
      });
      expect(withRecord.motorChecks.main_hand.records).toHaveLength(1);
      // 途中経過なので進捗も開始時刻もそのまま
      expect(withRecord.motorChecks.main_hand.status).toBe("running");
      expect(withRecord.motorChecks.main_hand.startedAtMs).toBe(NOW);

      const done = receive(withRecord, {
        type: "motor_check_done",
        robot: "main_hand",
        snapshot: {
          robot: "main_hand",
          started_at: 10,
          finished_at: 20,
          overall: "ok",
          records: [{ motor: "lift", result: "passed" }],
        },
      });
      const state = done.motorChecks.main_hand;
      expect(state.status).toBe("completed");
      expect(state.current).toBeNull();
      // ワイヤはエポック秒。UI 状態は ms でなければ実施時刻が 1970 年になる
      expect(state.startedAtMs).toBe(10_000);
      expect(state.finishedAtMs).toBe(20_000);
    });

    it("done に時刻が無ければ受信時刻と直前の開始時刻で補う", () => {
      const running = receive(INITIAL_ROBOT_UI_STATE, {
        type: "motor_check_progress",
        robot: "main_hand",
        index: 0,
        total: 1,
      });
      const done = receive(
        running,
        {
          type: "motor_check_done",
          robot: "main_hand",
          snapshot: {
            robot: "main_hand",
            started_at: null,
            finished_at: null,
            overall: "ok",
            records: [],
          },
        },
        NOW + 5000,
      );

      expect(done.motorChecks.main_hand.startedAtMs).toBe(NOW);
      expect(done.motorChecks.main_hand.finishedAtMs).toBe(NOW + 5000);
    });

    it("error は実行中の表示を残さず失敗として畳む", () => {
      const running = receive(INITIAL_ROBOT_UI_STATE, {
        type: "motor_check_progress",
        robot: "main_hand",
        current: "lift",
        index: 0,
        total: 2,
      });
      const failed = receive(running, {
        type: "motor_check_error",
        robot: "main_hand",
        message: "CAN タイムアウト",
      });

      const state = failed.motorChecks.main_hand;
      expect(state.status).toBe("error");
      expect(state.error).toBe("CAN タイムアウト");
      expect(state.current).toBeNull();
      expect(state.finishedAtMs).toBe(NOW);
    });

    it("ロボットごとに独立した実行状態を持つ", () => {
      const a = receive(INITIAL_ROBOT_UI_STATE, {
        type: "motor_check_progress",
        robot: "main_hand",
        index: 0,
        total: 1,
      });
      const b = receive(a, { type: "motor_check_error", robot: "sub_hand", message: "ng" });

      expect(b.motorChecks.main_hand.status).toBe("running");
      expect(b.motorChecks.sub_hand.status).toBe("error");
    });
  });

  it("ヘルス変化は新しい順に 5 件だけ残す", () => {
    let state = INITIAL_ROBOT_UI_STATE;
    for (let i = 0; i < 8; i++) {
      state = receive(state, { type: "health_change", robot: "main_hand", target: `m${i}` });
    }
    expect(state.healthEvents.map((e) => e.target)).toEqual(["m7", "m6", "m5", "m4", "m3"]);
  });
});
