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

  it("動作確認は継ぎ足さず、届いた状態でまるごと置き換える", () => {
    // UI 側で進捗を組み立てると、1 通落としたときに画面だけが古い状態で固まる。
    // 再送も無いのでリロードするまで直らない
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
    // 前の current_step が残らないこと (継ぎ足していたら残る)
    expect(finished.motorCheck.current_step).toBeNull();
  });

  it("ヘルス変化は新しい順に 5 件だけ残す", () => {
    let state = INITIAL_ROBOT_UI_STATE;
    for (let i = 0; i < 8; i++) {
      state = receive(state, { type: "health_change", robot: "main_hand", target: `m${i}` });
    }
    expect(state.healthEvents.map((e) => e.target)).toEqual(["m7", "m6", "m5", "m4", "m3"]);
  });

  describe("ステップ応答の保持", () => {
    // この describe の外で使う場面が無いので、内側に置いたままにする
    // oxlint-disable-next-line unicorn/consistent-function-scoping
    const capture = (motor: string, kp: number, robot = "main_hand") => ({
      type: "tuning_capture",
      robot,
      motor,
      captured_at: 1,
      gains: { kp, ki: 0, kd: 0 },
      metrics: null,
      advice: [],
      samples: { t: [0, 1], target: [10, 10], pos: [0, 10], output: [1, 1], sat: [false, false] },
    });

    it("モータごとに保持する", () => {
      const state = receive(INITIAL_ROBOT_UI_STATE, capture("y_axis_r", 2));
      expect(state.tuningCaptures["main_hand/y_axis_r"]).toHaveLength(1);
    });

    it("ロボットが違えば別のキーになる", () => {
      /** モータ名はロボット横断に一意だが、画面はロボットごとに分けて出す */
      let state = receive(INITIAL_ROBOT_UI_STATE, capture("lift", 2, "main_hand"));
      state = receive(state, capture("lift", 3, "sub_hand"));
      expect(Object.keys(state.tuningCaptures).toSorted()).toEqual([
        "main_hand/lift",
        "sub_hand/lift",
      ]);
    });

    it("新しい順に 2 件だけ残す", () => {
      /** 「変える前より良くなったか」に答えるには前回の 1 件が要る。3 件目は出す場所が無い */
      let state = INITIAL_ROBOT_UI_STATE;
      for (const kp of [1, 2, 3]) state = receive(state, capture("y_axis_r", kp));

      const kept = state.tuningCaptures["main_hand/y_axis_r"];
      expect(kept.map((c) => c.gains.kp)).toEqual([3, 2]);
    });

    it("他モータの配列は参照ごと据え置く", () => {
      /** 無関係な記録でグラフを描き直さない */
      const first = receive(INITIAL_ROBOT_UI_STATE, capture("y_axis_r", 2));
      const next = receive(first, capture("y_axis_l", 2));

      expect(next.tuningCaptures["main_hand/y_axis_r"]).toBe(
        first.tuningCaptures["main_hand/y_axis_r"],
      );
    });

    it("テレメトリの参照を作り直さない", () => {
      const first = receive(INITIAL_ROBOT_UI_STATE, {
        type: "state",
        robot: "main_hand",
        step_index: 1,
      });
      const next = receive(first, capture("y_axis_r", 2));

      expect(next.states).toBe(first.states);
      expect(next.matchState).toBe(first.matchState);
    });

    it("state 受信で記録の参照を作り直さない", () => {
      const first = receive(INITIAL_ROBOT_UI_STATE, capture("y_axis_r", 2));
      const next = receive(first, { type: "state", robot: "main_hand", step_index: 1 });

      expect(next.tuningCaptures).toBe(first.tuningCaptures);
    });
  });
});
