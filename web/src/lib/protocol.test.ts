import { describe, expect, it } from "vitest";

import { parseServerMessage } from "@/lib/protocol";

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
  });

  describe("match_state", () => {
    it("サーバー値をそのまま試合状態にする", () => {
      expect(
        parse({
          type: "match_state",
          court: "blue",
          phase: "match",
          can_start_match: true,
          checklists: { main_hand: { items: [], completed: true } },
        }),
      ).toEqual({
        type: "match_state",
        matchState: {
          court: "blue",
          phase: "match",
          can_start_match: true,
          checklists: { main_hand: { items: [], completed: true } },
        },
      });
    });

    it("checklists / can_start_match が欠けても既定値で成立させる", () => {
      const msg = parse({ type: "match_state", court: "red", phase: "ready" });
      expect(msg).toMatchObject({
        matchState: { checklists: {}, can_start_match: false },
      });
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

  describe("motor_check_*", () => {
    it("progress は index / total の欠落を 0 で埋める", () => {
      expect(parse({ type: "motor_check_progress", robot: "main_hand" })).toEqual({
        type: "motor_check_progress",
        robot: "main_hand",
        current: null,
        index: 0,
        total: 0,
      });
    });

    it("record / snapshot がオブジェクトでなければ捨てる", () => {
      expect(parse({ type: "motor_check_record", robot: "main_hand" })).toBeNull();
      expect(parse({ type: "motor_check_record", robot: "main_hand", record: 1 })).toBeNull();
      expect(parse({ type: "motor_check_done", robot: "main_hand" })).toBeNull();
    });

    it("error は message 省略時に既定文を入れる", () => {
      expect(parse({ type: "motor_check_error", robot: "main_hand" })).toEqual({
        type: "motor_check_error",
        robot: "main_hand",
        message: "unknown error",
      });
    });

    it("robot を持たない motor_check_* は捨てる", () => {
      expect(parse({ type: "motor_check_progress" })).toBeNull();
      expect(parse({ type: "motor_check_error", message: "ng" })).toBeNull();
    });
  });
});
