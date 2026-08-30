import { describe, expect, it } from "vitest";

import { parseServerMessage } from "@/lib/protocol";
import type { RobotState } from "@/lib/protocol";

/**
 * 受信条件そのもののテスト。
 *
 * 型は実行時に消えるので、「サーバーが送っているのに UI が捨てる」事故は
 * 受信条件を直接固定する以外に守れない (`health_change` の `robot` を
 * 必須にしていて実機で 100% 捨てていた前例がある)。
 * 実配信サンプルとの突き合わせは `test/wsContract.test.ts` が担う。
 */
describe("parseServerMessage", () => {
  it("JSON として壊れた入力を null にする", () => {
    expect(parseServerMessage("{ not json")).toBeNull();
  });

  it("未知の type を null にする", () => {
    expect(parseServerMessage(JSON.stringify({ type: "unknown_event" }))).toBeNull();
  });

  it("type を持たない入力を null にする", () => {
    expect(parseServerMessage(JSON.stringify({ robot: "main_hand" }))).toBeNull();
    expect(parseServerMessage(JSON.stringify(42))).toBeNull();
  });

  function parse(payload: object) {
    return parseServerMessage(JSON.stringify(payload));
  }

  describe("state", () => {
    it("robot 付きの state を受ける (配信内容はそのまま保持する)", () => {
      const msg = parse({ type: "state", robot: "main_hand", step_index: 3 });
      expect(msg).toEqual({
        type: "state",
        robot: "main_hand",
        state: { type: "state", robot: "main_hand", step_index: 3 },
      });
    });

    it("robot の無い state は捨てる (どのロボットの状態か決められない)", () => {
      expect(parse({ type: "state", step_index: 3 })).toBeNull();
    });

    it("ヘルスの detail を受信経路で落とさない", () => {
      // サーバーは健全性を計算できなかったとき overall=down と detail だけで
      // 「判定不能」を伝える。detail を捨てると画面に理由が残らない
      const msg = parse({
        type: "state",
        robot: "main_hand",
        health: { timestamp: 0, overall: "down", buses: [], motors: [], detail: "計算失敗" },
      });
      expect(msg).not.toBeNull();
      const state = (msg as { state: RobotState }).state;
      expect(state.health?.detail).toBe("計算失敗");
    });
  });

  describe("server_info", () => {
    it("開発用フラグをそのまま持つ", () => {
      expect(parse({ type: "server_info", dev_tools: true, dry_run: true })).toEqual({
        type: "server_info",
        serverInfo: {
          dev_tools: true,
          dry_run: true,
          temp_warning_c: null,
          temp_critical_c: null,
        },
      });
    });

    it("フラグが欠けていたら無効に倒す", () => {
      // 開発用ボタンが本番で出るより、開発用起動で出ない方が安全側
      expect(parse({ type: "server_info" })).toEqual({
        type: "server_info",
        serverInfo: {
          dev_tools: false,
          dry_run: false,
          temp_warning_c: null,
          temp_critical_c: null,
        },
      });
    });

    it("真偽値以外を真として扱わない", () => {
      expect(parse({ type: "server_info", dev_tools: "true", dry_run: 1 })).toEqual({
        type: "server_info",
        serverInfo: {
          dev_tools: false,
          dry_run: false,
          temp_warning_c: null,
          temp_critical_c: null,
        },
      });
    });

    it("温度しきい値をそのまま持つ", () => {
      // UI 側に既定値を置かないので、config の値はこの 1 通でしか入らない
      expect(parse({ type: "server_info", temp_warning_c: 65, temp_critical_c: 80 })).toMatchObject(
        {
          serverInfo: { temp_warning_c: 65, temp_critical_c: 80 },
        },
      );
    });

    it("しきい値が number でなければ null (代わりの既定値を持たない)", () => {
      // 数値でない値を通すと比較が常に false になり、警告が一切出ないまま
      // 「しきい値は届いている」ように見える
      expect(
        parse({ type: "server_info", temp_warning_c: "65", temp_critical_c: null }),
      ).toMatchObject({
        serverInfo: { temp_warning_c: null, temp_critical_c: null },
      });
    });
  });

  describe("match_state", () => {
    it("サーバー値をそのまま試合状態にする", () => {
      expect(
        parse({
          type: "match_state",
          court: "blue",
          phase: "match",
          can_start_match: true,
          checklists: { pre_match: { items: [], completed: true } },
          timer: { running: true, elapsed_ms: 12_000, duration_ms: 180_000 },
        }),
      ).toEqual({
        type: "match_state",
        matchState: {
          court: "blue",
          phase: "match",
          can_start_match: true,
          checklists: { pre_match: { items: [], completed: true } },
          timer: { running: true, elapsed_ms: 12_000, duration_ms: 180_000 },
        },
      });
    });

    it("checklists / can_start_match が欠けても既定値で成立させる", () => {
      const msg = parse({ type: "match_state", court: "red", phase: "ready" });
      expect(msg).toMatchObject({
        matchState: { checklists: {}, can_start_match: false },
      });
    });

    it("タイマーが欠けても match_state ごと捨てない", () => {
      // フェーズと指差喚呼の進捗は試合の進行そのものを握っている。タイマーが
      // 読めないという理由でそちらまで落とすほうがはるかに悪い
      const msg = parse({ type: "match_state", court: "red", phase: "match" });

      expect(msg).toMatchObject({ matchState: { phase: "match", timer: null } });
    });

    it.each([
      ["running が boolean でない", { running: "yes", elapsed_ms: 0, duration_ms: 180_000 }],
      ["elapsed_ms が無い", { running: true, duration_ms: 180_000 }],
      ["duration_ms が無い", { running: true, elapsed_ms: 0 }],
      ["duration_ms が 0", { running: true, elapsed_ms: 0, duration_ms: 0 }],
      ["duration_ms が負", { running: true, elapsed_ms: 0, duration_ms: -1 }],
    ])("壊れたタイマー (%s) は null にする", (_label, timer) => {
      // duration_ms <= 0 を通すと残り時間が常に 0 以下になり、
      // 画面には「試合開始と同時に時間切れ」が出る
      const msg = parse({ type: "match_state", court: "red", phase: "match", timer });

      expect(msg).toMatchObject({ matchState: { timer: null } });
    });
  });

  describe("e_stop_state", () => {
    it("active と理由を運ぶ", () => {
      expect(parse({ type: "e_stop_state", active: true, reason: "同期ずれ" })).toEqual({
        type: "e_stop_state",
        active: true,
        reason: "同期ずれ",
      });
    });

    it("解除時は理由を持たない", () => {
      expect(parse({ type: "e_stop_state", active: false, reason: "同期ずれ" })).toEqual({
        type: "e_stop_state",
        active: false,
        reason: null,
      });
    });

    it("active が真偽値でなければ捨てる", () => {
      expect(parse({ type: "e_stop_state", active: "yes" })).toBeNull();
    });
  });

  describe("command_rejected", () => {
    it("command / reason が欠けても空文字で受ける (拒否を握り潰さない)", () => {
      expect(parse({ type: "command_rejected" })).toEqual({
        type: "command_rejected",
        command: "",
        reason: "",
      });
    });
  });

  describe("health_change", () => {
    it("level 省略時は info 扱いにする", () => {
      expect(parse({ type: "health_change", robot: "main_hand", target: "can0" })).toEqual({
        type: "health_change",
        event: {
          robot: "main_hand",
          level: "info",
          target: "can0",
          from: "",
          to: "",
          message: "",
        },
      });
    });

    it("robot を持たない health_change は捨てる", () => {
      expect(parse({ type: "health_change", target: "can0" })).toBeNull();
    });
  });

  describe("motor_check_state", () => {
    it("robot を要求しない (両ハンド統合の 1 本なので載っていない)", () => {
      // ここで robot を必須にすると動作確認の状態が 100% 捨てられる。
      // health_change で実際にやらかした形なので、受信条件として固定する
      const message = parse({ type: "motor_check_state", available: true, running: false });

      expect(message).not.toBeNull();
      expect(message?.type).toBe("motor_check_state");
    });

    it("欠けたフィールドを安全側の既定で埋める", () => {
      const message = parse({ type: "motor_check_state" });

      expect(message).toEqual({
        type: "motor_check_state",
        motorCheck: {
          // available は「押せる」へ倒さない (押しても拒否されるボタンを出さない)
          available: false,
          blocked_reason: null,
          running: false,
          current_step: null,
          step_index: 0,
          total_steps: 0,
          steps: [],
          error: null,
        },
      });
    });

    it("ステップ表と進捗をそのまま運ぶ", () => {
      const message = parse({
        type: "motor_check_state",
        available: true,
        blocked_reason: null,
        running: true,
        current_step: "メインハンド y 軸",
        step_index: 1,
        total_steps: 2,
        steps: [
          { index: 0, label: "メインハンド 初期姿勢へ", require_trigger: false },
          { index: 1, label: "メインハンド y 軸", require_trigger: false },
        ],
        error: null,
      });

      expect(message?.type).toBe("motor_check_state");
      if (message?.type !== "motor_check_state") return;
      expect(message.motorCheck.running).toBe(true);
      expect(message.motorCheck.steps).toHaveLength(2);
      expect(message.motorCheck.current_step).toBe("メインハンド y 軸");
    });

    it("steps が配列でなければ空配列にする", () => {
      const message = parse({ type: "motor_check_state", steps: "壊れた値" });

      expect(message?.type).toBe("motor_check_state");
      if (message?.type !== "motor_check_state") return;
      expect(message.motorCheck.steps).toEqual([]);
    });
  });

  describe("tuning_capture", () => {
    const payload = (overrides: object = {}) => ({
      type: "tuning_capture",
      robot: "main_hand",
      motor: "y_axis_r",
      captured_at: 1700000000,
      gains: { kp: 2, ki: 0, kd: 0 },
      metrics: null,
      advice: [],
      samples: { t: [0, 1], target: [10, 10], pos: [0, 9], output: [500, 100], sat: [true, false] },
      ...overrides,
    });

    it("波形・指標・助言を 1 通で受け取る", () => {
      const message = parse(
        payload({
          metrics: { overshoot_pct: 12 },
          advice: [{ code: "overshoot", severity: "info", message: "行き過ぎ" }],
        }),
      );

      expect(message?.type).toBe("tuning_capture");
      if (message?.type !== "tuning_capture") return;
      expect(message.capture.samples.pos).toEqual([0, 9]);
      expect(message.capture.metrics).not.toBeNull();
      expect(message.capture.advice).toHaveLength(1);
    });

    it("指標が null の記録も受け取る", () => {
      /** 弾いてしまうと、波形だけは見たい場面で画面に何も出ない */
      const message = parse(payload());

      expect(message?.type).toBe("tuning_capture");
      if (message?.type !== "tuning_capture") return;
      expect(message.capture.metrics).toBeNull();
    });

    it("列の長さが揃っていない波形は捨てる", () => {
      /**
       * 揃っていない列を描くと、`t` の長さでループした先で `pos` が undefined になり、
       * 例外も出ないままグラフだけが静かに途切れる。
       */
      const message = parse(
        payload({
          samples: { t: [0, 1], target: [10], pos: [0, 9], output: [1, 1], sat: [false] },
        }),
      );

      expect(message).toBeNull();
    });

    it("波形そのものが欠けていたら捨てる", () => {
      expect(parse(payload({ samples: undefined }))).toBeNull();
    });

    it("robot が無ければ捨てる", () => {
      /** 画面はロボットごとに分けて出すので、宛先の無い記録は置き場所が無い */
      expect(parse(payload({ robot: undefined }))).toBeNull();
    });
  });
});
