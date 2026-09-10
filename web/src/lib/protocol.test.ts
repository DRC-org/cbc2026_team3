import { describe, expect, it } from "vitest";

import { readableHealth } from "@/lib/healthVerdict";
import { MALFORMED, parseServerMessage, readCommand, readMeasured } from "@/lib/protocol";
import type { RobotState } from "@/lib/protocol";

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
        state: { type: "state", robot: "main_hand", step_index: 3, last_error: null },
      });
    });

    it("robot の無い state は捨てる (どのロボットの状態か決められない)", () => {
      expect(parse({ type: "state", step_index: 3 })).toBeNull();
    });

    it("ヘルスの detail を受信経路で落とさない", () => {
      const msg = parse({
        type: "state",
        robot: "main_hand",
        health: { timestamp: 0, overall: "down", buses: [], motors: [], detail: "計算失敗" },
      });
      expect(msg).not.toBeNull();
      const state = (msg as { state: RobotState }).state;
      expect(readableHealth(state.health)?.detail).toBe("計算失敗");
    });

    describe("health", () => {
      const HEALTH = {
        timestamp: 0,
        overall: "ok",
        buses: [{ name: "can_m3508", state: "ok" }],
        motors: [{ name: "y_axis_r", state: "ok" }],
        detail: null,
      };

      const healthOf = (health: unknown) => {
        const msg = parse({ type: "state", robot: "main_hand", health });
        return (msg as { state: RobotState }).state.health;
      };

      it("読める配信はそのまま持つ (組み立て直さない)", () => {
        expect(healthOf(HEALTH)).toEqual(HEALTH);
      });

      it.each(["buses", "motors", "overall"])("%s が欠けたら MALFORMED", (key) => {
        const broken: Record<string, unknown> = { ...HEALTH };
        delete broken[key];
        expect(healthOf(broken)).toBe(MALFORMED);
      });

      it("未知の state が載っていたら MALFORMED (ok と同じ扱いにしない)", () => {
        expect(healthOf({ ...HEALTH, buses: [{ name: "can_m3508", state: "exploded" }] })).toBe(
          MALFORMED,
        );
      });

      it("未配信は MALFORMED にしない (届いていないことと読めないことは別)", () => {
        expect(healthOf(undefined)).toBeUndefined();
      });
    });

    describe("safety", () => {
      const SAFETY = {
        sync_violations: [],
        unenergized_motors: [],
        unresponsive_motors: [],
        firmware_unconfirmed_motors: [],
        failed_tasks: [],
        reenergizing: false,
        loops_running: true,
        monitors_running: true,
        limit_monitors_running: true,
        refreshers_running: true,
        position_loops: [{ bus: "can_m3508", running: true, paused: false, sync_violations: [] }],
        sync_monitors: [{ axes: ["y_axis"], running: true, violated: [] }],
        limit_monitors: [{ axes: ["sub_y_axis"], running: true, stopped: [] }],
        target_refreshers: [{ motors: ["gripper"], running: true, paused: false }],
      };

      it("読める配信は配信オブジェクトのまま通す (未使用欄も落とさない)", () => {
        const msg = parse({ type: "state", robot: "main_hand", safety: SAFETY });
        expect((msg as { state: RobotState }).state.safety).toEqual(SAFETY);
      });

      it("未配信は undefined のまま (未受信は異常にしない)", () => {
        const msg = parse({ type: "state", robot: "main_hand" });
        expect((msg as { state: RobotState }).state.safety).toBeUndefined();
      });

      it.each([
        "sync_violations",
        "unenergized_motors",
        "unresponsive_motors",
        "firmware_unconfirmed_motors",
        "failed_tasks",
        "reenergizing",
        "loops_running",
        "monitors_running",
        "limit_monitors_running",
        "refreshers_running",
        "position_loops",
        "sync_monitors",
        "limit_monitors",
        "target_refreshers",
      ])("%s が欠けたら MALFORMED (空の SafetyState へ倒さない)", (key) => {
        const broken: Record<string, unknown> = { ...SAFETY };
        delete broken[key];
        const msg = parse({ type: "state", robot: "main_hand", safety: broken });
        expect((msg as { state: RobotState }).state.safety).toBe(MALFORMED);
      });
    });

    describe("manual の positions", () => {
      const manualOf = (positions: unknown) => {
        const msg = parse({
          type: "state",
          robot: "main_hand",
          manual: { mode: "manual", axes: [{ name: "y_axis", positions }] },
        });
        return (msg as { state: RobotState }).state.manual?.axes[0].positions;
      };

      it("現行の形はそのまま通す", () => {
        expect(
          manualOf([
            { name: "home", value: 0 },
            { name: "work", value: 10 },
          ]),
        ).toEqual([
          { name: "home", value: 0 },
          { name: "work", value: 10 },
        ]);
      });

      it("値が引けなかった位置の null を保つ (0 へ寄せない)", () => {
        expect(manualOf([{ name: "place", value: null }])).toEqual([
          { name: "place", value: null },
        ]);
      });

      it("旧サーバーの素の文字列を value: null として受ける", () => {
        expect(manualOf(["home", "work"])).toEqual([
          { name: "home", value: null },
          { name: "work", value: null },
        ]);
      });

      it("どちらの形でもない要素だけ落とす", () => {
        expect(manualOf([{ name: "home", value: 0 }, { value: 3 }, 42, null])).toEqual([
          { name: "home", value: 0 },
        ]);
      });

      it("値が数値でも null でもない要素も落とす", () => {
        expect(
          manualOf([
            { name: "home", value: 0 },
            { name: "work", value: {} },
            { name: "place", value: "10" },
            { name: "pick" },
          ]),
        ).toEqual([{ name: "home", value: 0 }]);
      });

      it("軸の他の欄は素通しのまま (軸名も可動範囲も UI へ書かない)", () => {
        const msg = parse({
          type: "state",
          robot: "main_hand",
          manual: {
            mode: "manual",
            axes: [{ name: "y_axis", unit: "mm", manual: { min: 0, max: 1, steps: [1] } }],
          },
        });
        expect((msg as { state: RobotState }).state.manual).toEqual({
          mode: "manual",
          axes: [{ name: "y_axis", unit: "mm", manual: { min: 0, max: 1, steps: [1] } }],
        });
      });

      it("未配信は undefined のまま (手動を配らない版のサーバーを異常にしない)", () => {
        const msg = parse({ type: "state", robot: "main_hand" });
        expect((msg as { state: RobotState }).state.manual).toBeUndefined();
      });
    });

    describe("sensors", () => {
      const SENSORS = {
        origin_sensor: { active: true, stale: false },
        rotate_origin_sensor: { active: false, stale: true },
      };

      const sensorsOf = (sensors: unknown) => {
        const msg = parse({ type: "state", robot: "main_hand", sensors });
        return (msg as { state: RobotState }).state.sensors;
      };

      it("読める配信はセンサ名ごとそのまま通す (名前を UI へ書き写さない)", () => {
        expect(sensorsOf(SENSORS)).toEqual(SENSORS);
      });

      it("センサを 1 本も持たない構成は空のまま (異常にしない)", () => {
        expect(sensorsOf({})).toEqual({});
      });

      it("未配信は undefined のまま (古いサーバーを異常にしない)", () => {
        const msg = parse({ type: "state", robot: "main_hand" });
        expect((msg as { state: RobotState }).state.sensors).toBeUndefined();
      });

      it("接触を報告できないドライバの null を受ける (false と混ぜない)", () => {
        const sensors = { origin_sensor: { active: null, stale: false } };
        expect(sensorsOf(sensors)).toEqual(sensors);
      });

      it.each(["active", "stale"])("%s が欠けたら MALFORMED (空へ倒さない)", (key) => {
        const broken: Record<string, unknown> = { active: true, stale: false };
        delete broken[key];
        expect(sensorsOf({ origin_sensor: broken })).toBe(MALFORMED);
      });

      it("stale が真偽値でなければ MALFORMED", () => {
        expect(sensorsOf({ origin_sensor: { active: true, stale: "no" } })).toBe(MALFORMED);
      });

      it("オブジェクトでない配信は MALFORMED", () => {
        expect(sensorsOf("origin_sensor")).toBe(MALFORMED);
      });
    });

    describe("suction", () => {
      const SUCTION = {
        pads: [
          { axis: "valve_1", label: "1", enabled: true },
          { axis: "valve_2", label: "2", enabled: false },
        ],
      };

      const suctionOf = (suction: unknown) => {
        const msg = parse({ type: "state", robot: "sub_hand", suction });
        return (msg as { state: RobotState }).state.suction;
      };

      it("読める配信はパッドごとそのまま通す (弁の名前を UI へ書き写さない)", () => {
        expect(suctionOf(SUCTION)).toEqual(SUCTION);
      });

      it("吸着パッドを持たないロボットの null はそのまま (異常にしない)", () => {
        expect(suctionOf(null)).toBeNull();
      });

      it("未配信は undefined のまま (古いサーバーを異常にしない)", () => {
        const msg = parse({ type: "state", robot: "sub_hand" });
        expect((msg as { state: RobotState }).state.suction).toBeUndefined();
      });

      it.each(["axis", "label", "enabled"])("%s が欠けたら MALFORMED (空へ倒さない)", (key) => {
        const broken: Record<string, unknown> = { axis: "valve_1", label: "1", enabled: true };
        delete broken[key];
        expect(suctionOf({ pads: [broken] })).toBe(MALFORMED);
      });

      it("enabled が真偽値でなければ MALFORMED (押せるボタンを配信の崩れで増やさない)", () => {
        expect(suctionOf({ pads: [{ axis: "valve_1", label: "1", enabled: "yes" }] })).toBe(
          MALFORMED,
        );
      });

      it("pads が配列でなければ MALFORMED", () => {
        expect(suctionOf({ pads: "valve_1" })).toBe(MALFORMED);
        expect(suctionOf("valve_1")).toBe(MALFORMED);
      });
    });

    it("motors と steps は知らないモータ・欄ごとそのまま通す", () => {
      const motors = {
        brand_new_motor: { pos: 1, vel: 2, torque: 3, temp: 4, future_field: "keep" },
      };
      const steps = [{ index: 0, label: "把持", require_trigger: true, future_field: 1 }];
      const msg = parse({ type: "state", robot: "main_hand", motors, steps });

      const state = (msg as { state: RobotState }).state;
      expect(state.motors).toEqual(motors);
      expect(state.steps).toEqual(steps);
    });

    it("測れない項目が null のモータを異常扱いにしない", () => {
      const motors = {
        conveyor: { pos: null, vel: null, torque: null, temp: null, command: 0.3 },
        y_axis_r: { pos: 1.5, vel: 0, torque: 0.2, temp: 41, command: 1.5 },
      };
      const msg = parse({ type: "state", robot: "main_hand", motors });

      expect(msg).not.toBeNull();
      expect((msg as { state: RobotState }).state.motors).toEqual(motors);
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
      expect(parse({ type: "server_info", temp_warning_c: 65, temp_critical_c: 80 })).toMatchObject(
        {
          serverInfo: { temp_warning_c: 65, temp_critical_c: 80 },
        },
      );
    });

    it("しきい値が number でなければ null (代わりの既定値を持たない)", () => {
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

    it.each([
      ["items が配列でない", { pre_match: { items: null, completed: false } }],
      ["completed が無い", { pre_match: { items: [] } }],
      [
        "項目の checked が boolean でない",
        {
          pre_match: { items: [{ id: "a", label: "A", checked: "yes" }], completed: false },
        },
      ],
      ["そもそもオブジェクトでない", "pre_match"],
    ])("checklists が読めない形なら MALFORMED (%s)", (_name, checklists) => {
      const msg = parse({ type: "match_state", court: "red", phase: "ready", checklists });
      expect(msg).toMatchObject({ matchState: { checklists: MALFORMED } });
    });

    it("読めない checklists でもフェーズは捨てない", () => {
      const msg = parse({ type: "match_state", court: "red", phase: "match", checklists: 7 });
      expect(msg).toMatchObject({ matchState: { phase: "match", court: "red" } });
    });

    it("タイマーが欠けても match_state ごと捨てない", () => {
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
    it("既知の 3 値はそのまま通す", () => {
      for (const level of ["info", "warning", "critical"] as const) {
        const msg = parse({ type: "health_change", robot: "main_hand", target: "can0", level });
        expect(msg).toMatchObject({ event: { level } });
      }
    });

    it("level 省略時は critical (異常側) へ倒す", () => {
      expect(parse({ type: "health_change", robot: "main_hand", target: "can0" })).toEqual({
        type: "health_change",
        event: {
          robot: "main_hand",
          level: "critical",
          target: "can0",
          from: "",
          to: "",
          message: "",
        },
      });
    });

    it.each([[42], [{ x: 1 }], [["critical"]], [null], [true]])(
      "非文字列の level (%j) も critical へ倒す (無検査キャストで画面が落ちないように)",
      (level) => {
        const msg = parse({ type: "health_change", robot: "main_hand", target: "can0", level });
        expect(msg).toMatchObject({ event: { level: "critical" } });
      },
    );

    it("未知の文字列の level も critical へ倒す", () => {
      const msg = parse({
        type: "health_change",
        robot: "main_hand",
        target: "can0",
        level: "debug",
      });
      expect(msg).toMatchObject({ event: { level: "critical" } });
    });

    it("robot を持たない health_change は捨てる", () => {
      expect(parse({ type: "health_change", target: "can0" })).toBeNull();
    });
  });

  describe("match_state のコートとフェーズ", () => {
    const base = { type: "match_state", can_start_match: false };

    const matchStateOf = (payload: object) =>
      (parse(payload) as unknown as { matchState: Record<string, unknown> }).matchState;

    it("既知の値はそのまま通す", () => {
      const msg = parse({ ...base, court: "blue", phase: "match" });
      expect(msg).toMatchObject({ matchState: { court: "blue", phase: "match" } });
    });

    it.each([
      ["court", { ...base, court: "green", phase: "match" }],
      ["phase", { ...base, court: "red", phase: "paused" }],
    ])("未知の %s は MALFORMED にする (既定値へ倒さない)", (key, payload) => {
      expect(matchStateOf(payload)[key]).toBe(MALFORMED);
    });

    it("court の null は未確定として通す (MALFORMED へ潰さない)", () => {
      expect(matchStateOf({ ...base, court: null, phase: "setup" }).court).toBeNull();
    });

    it("欠落も MALFORMED にする", () => {
      expect(matchStateOf(base).court).toBe(MALFORMED);
      expect(matchStateOf(base).phase).toBe(MALFORMED);
    });

    it("片方が読めなくてももう片方は落とさない", () => {
      expect(matchStateOf({ ...base, court: "green", phase: "match" }).phase).toBe("match");
    });
  });

  describe("motor_check_state", () => {
    it("robot を要求しない (両ハンド統合の 1 本なので載っていない)", () => {
      const message = parse({ type: "motor_check_state", available: true, running: false });

      expect(message).not.toBeNull();
      expect(message?.type).toBe("motor_check_state");
    });

    it("欠けたフィールドを安全側の既定で埋める", () => {
      const message = parse({ type: "motor_check_state" });

      expect(message).toEqual({
        type: "motor_check_state",
        motorCheck: {
          available: false,
          blocked_reason: null,
          running: false,
          current_step: null,
          step_index: 0,
          total_steps: 0,
          steps: MALFORMED,
          error: null,
          last_error: null,
          excluded_steps: MALFORMED,
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

    it("steps が読めなければ MALFORMED へ倒す (空配列にしない)", () => {
      const message = parse({ type: "motor_check_state", steps: "壊れた値" });

      expect(message?.type).toBe("motor_check_state");
      if (message?.type !== "motor_check_state") return;
      expect(message.motorCheck.steps).toBe(MALFORMED);
    });

    it("steps の要素の形が違えば MALFORMED へ倒す", () => {
      const message = parse({
        type: "motor_check_state",
        steps: [{ index: 0, label: "ok", require_trigger: false }, null],
      });

      expect(message?.type).toBe("motor_check_state");
      if (message?.type !== "motor_check_state") return;
      expect(message.motorCheck.steps).toBe(MALFORMED);
    });

    it("除外したステップと欠けている軸をそのまま運ぶ", () => {
      const message = parse({
        type: "motor_check_state",
        excluded_steps: [{ step: "サブハンド 昇降", missing_axes: ["sub_lift"] }],
      });

      expect(message?.type).toBe("motor_check_state");
      if (message?.type !== "motor_check_state") return;
      expect(message.motorCheck.excluded_steps).toEqual([
        { step: "サブハンド 昇降", missing_axes: ["sub_lift"] },
      ]);
    });

    it("除外が無ければ空配列で受ける", () => {
      const message = parse({ type: "motor_check_state", excluded_steps: [] });

      expect(message?.type).toBe("motor_check_state");
      if (message?.type !== "motor_check_state") return;
      expect(message.motorCheck.excluded_steps).toEqual([]);
    });

    it("除外が読めない形なら MALFORMED へ倒す", () => {
      const message = parse({
        type: "motor_check_state",
        excluded_steps: [{ step: "サブハンド 昇降" }],
      });

      expect(message?.type).toBe("motor_check_state");
      if (message?.type !== "motor_check_state") return;
      expect(message.motorCheck.excluded_steps).toBe(MALFORMED);
    });
  });

  describe("switch_measure_state", () => {
    const RESULT = {
      axis: "sub_y_axis",
      unit: "mm",
      direction: -1,
      engage: -447.0,
      release: -441.2,
      width: 5.8,
      step: 0.5,
      coarse_step: null,
    };

    it("結果と対象をそのまま運ぶ", () => {
      const message = parse({
        type: "switch_measure_state",
        available: true,
        blocked_reason: null,
        running: false,
        robot: "sub_hand",
        axis: "sub_y_axis",
        direction: -1,
        result: RESULT,
        error: null,
        targets: { main_hand: ["y_axis"], sub_hand: ["sub_y_axis"] },
      });

      expect(message).toEqual({
        type: "switch_measure_state",
        switchMeasure: {
          available: true,
          blocked_reason: null,
          running: false,
          robot: "sub_hand",
          axis: "sub_y_axis",
          direction: -1,
          result: RESULT,
          error: null,
          targets: { main_hand: ["y_axis"], sub_hand: ["sub_y_axis"] },
        },
      });
    });

    it("未実行の result は null、数値が欠けた result は MALFORMED (0 で埋めない)", () => {
      const idle = parse({ type: "switch_measure_state", result: null, targets: {} });
      expect(idle?.type).toBe("switch_measure_state");
      if (idle?.type !== "switch_measure_state") return;
      expect(idle.switchMeasure.result).toBeNull();
      expect(idle.switchMeasure.direction).toBeNull();

      const broken = parse({
        type: "switch_measure_state",
        result: { ...RESULT, engage: "-447.0" },
        targets: {},
      });
      expect(broken?.type).toBe("switch_measure_state");
      if (broken?.type !== "switch_measure_state") return;
      expect(broken.switchMeasure.result).toBe(MALFORMED);
    });

    it("向きが ±1 以外なら結果ごと MALFORMED", () => {
      const message = parse({
        type: "switch_measure_state",
        result: { ...RESULT, direction: 0 },
        targets: {},
      });
      expect(message?.type).toBe("switch_measure_state");
      if (message?.type !== "switch_measure_state") return;
      expect(message.switchMeasure.result).toBe(MALFORMED);
    });
  });
});

describe("readMeasured", () => {
  it("測れた値はそのまま返す", () => {
    expect(readMeasured(41.2)).toBe(41.2);
    expect(readMeasured(0)).toBe(0);
    expect(readMeasured(-5)).toBe(-5);
  });

  it("null は測る手段が無いことの表現。MALFORMED へ倒さない", () => {
    expect(readMeasured(null)).toBeNull();
  });

  it("欄の欠落・型違いは MALFORMED (黙って null や 0 へ丸めない)", () => {
    expect(readMeasured(undefined)).toBe(MALFORMED);
    expect(readMeasured("41.2")).toBe(MALFORMED);
    expect(readMeasured({})).toBe(MALFORMED);
    expect(readMeasured(Number.NaN)).toBe(MALFORMED);
    expect(readMeasured(Number.POSITIVE_INFINITY)).toBe(MALFORMED);
  });
});

describe("readCommand", () => {
  it("未配信 (undefined) は「指令が無い」へ倒す。MALFORMED にしない", () => {
    expect(readCommand(undefined)).toBeNull();
  });

  it("型違いは MALFORMED のまま (未配信と混ぜない)", () => {
    expect(readCommand("0.3")).toBe(MALFORMED);
    expect(readCommand(Number.NaN)).toBe(MALFORMED);
  });

  it("値と null は readMeasured と同じ", () => {
    expect(readCommand(0.3)).toBe(0.3);
    expect(readCommand(0)).toBe(0);
    expect(readCommand(null)).toBeNull();
  });
});
