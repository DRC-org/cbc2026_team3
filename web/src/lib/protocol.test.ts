import { describe, expect, it } from "vitest";

import { readableHealth } from "@/lib/healthVerdict";
import { MALFORMED, parseServerMessage, readCommand, readMeasured } from "@/lib/protocol";
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
        // last_error だけは受信境界が形を確定させるので、配信に無くても null が載る
        state: { type: "state", robot: "main_hand", step_index: 3, last_error: null },
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
      expect(readableHealth(state.health)?.detail).toBe("計算失敗");
    });

    /**
     * `health` も UI が `.filter` を直に呼ぶ (`evaluateHealth`)。しかも呼び出し元の
     * 1 つ (`TabBar`) は `RouteErrorBoundary` の**外**にあるため、投げれば React
     * ツリーごとアンマウントして**ヘッダーの緊急停止ボタンまで消える**。
     */
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

    /**
     * `safety` だけは UI が `.length` / `.filter` を直に呼ぶので、受信境界で形を
     * 確定させる。ここを素通しにすると 1 欄欠けた配信でレンダーが投げ、React
     * ツリーごとアンマウントしてヘッダーの緊急停止ボタンまで消える。
     */
    describe("safety", () => {
      const SAFETY = {
        sync_violations: [],
        unenergized_motors: [],
        firmware_unconfirmed_motors: [],
        loops_running: true,
        monitors_running: true,
        refreshers_running: true,
        position_loops: [{ bus: "can_m3508", running: true, paused: false, sync_violations: [] }],
        sync_monitors: [{ axes: ["y_axis"], running: true, violated: [] }],
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
        "firmware_unconfirmed_motors",
        "loops_running",
        "monitors_running",
        "refreshers_running",
        "position_loops",
        "sync_monitors",
        "target_refreshers",
      ])("%s が欠けたら MALFORMED (空の SafetyState へ倒さない)", (key) => {
        const broken: Record<string, unknown> = { ...SAFETY };
        delete broken[key];
        const msg = parse({ type: "state", robot: "main_hand", safety: broken });
        expect((msg as { state: RobotState }).state.safety).toBe(MALFORMED);
      });
    });

    /**
     * センサ一覧も UI が `Object.entries` を直に呼ぶので受信境界で形を確定させる。
     * **接触 (`active`) は異常ではない** ので値そのものは素通しで、異常側へ倒すのは
     * 読めなかった配信だけ。
     */
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

    /**
     * **`motors` と `steps` は素通しのまま保つ。** モータ名を UI 側へ書かない性質は
     * 配信をそのまま状態へ入れることで成立しており、ここで組み立て直すと
     * モータが 1 基増えるたびに UI の変更が要る形へ逆戻りする。
     */
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

    /**
     * **測れない項目の null は正当な測定結果であって、読めなかった配信ではない。**
     * 自作モータドライバの DC 基板・電磁弁基板は 4 値とも測る手段が無く、
     * サーボ基板は位置しか持たない。ここを異常扱いにすると、DC 基板を 1 枚
     * 積んだだけでそのロボットの state 配信が丸ごと捨てられる。
     */
    it("測れない項目が null のモータを異常扱いにしない", () => {
      const motors = {
        conveyor: { pos: null, vel: null, torque: null, temp: null, target: null, pid: null },
        y_axis_r: { pos: 1.5, vel: 0, torque: 0.2, temp: 41, target: 1.5, pid: null },
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
      // **空へ倒してはならない。** 空は「config に項目が無い」の表現として既に
      // 使っており、混ぜると操縦者は config/checklist.yaml を疑って探しに行く
      const msg = parse({ type: "match_state", court: "red", phase: "ready", checklists });
      expect(msg).toMatchObject({ matchState: { checklists: MALFORMED } });
    });

    it("読めない checklists でもフェーズは捨てない", () => {
      // タイマーと同じ理由。試合の進行そのものを握っている値を巻き添えにしない
      const msg = parse({ type: "match_state", court: "red", phase: "match", checklists: 7 });
      expect(msg).toMatchObject({ matchState: { phase: "match", court: "red" } });
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
    it("既知の 3 値はそのまま通す", () => {
      for (const level of ["info", "warning", "critical"] as const) {
        const msg = parse({ type: "health_change", robot: "main_hand", target: "can0", level });
        expect(msg).toMatchObject({ event: { level } });
      }
    });

    /**
     * `level` は `HealthChangeLevel`（3 値の union）で `MALFORMED` という
     * 第 4 の値を持てない。読めなかったときに軽い側 (`"info"`) へ倒すと、
     * 型不正のせいで本当に critical なイベントが画面から消える
     * (`web/src/lib/protocol.ts:967` で実際に `?? "info"` になっていた事故)。
     * ここでは異常側の `"critical"` へ倒すことを固定する。
     */
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

  /**
   * `court` / `phase` はどちらも `Record` の索引として使われる
   * (`PHASE_LABEL[phase]` / `COURT_TONE[court]`)。無検査キャストのままだと未知の値で
   * 索引が undefined になり、**フェーズチップとコートチップが無地・無文字で消える**。
   * さらに `isDuringMatch()` が false になって全画面が「準備中」へ倒れ、
   * 読めなかったこと自体が画面のどこにも現れない。
   */
  describe("match_state のコートとフェーズ", () => {
    const base = { type: "match_state", can_start_match: false };

    /** 受信後の match_state。読めなかった欄も値として載るので広い型で受ける */
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

    it("欠落も MALFORMED にする", () => {
      expect(matchStateOf(base).court).toBe(MALFORMED);
      expect(matchStateOf(base).phase).toBe(MALFORMED);
    });

    it("片方が読めなくてももう片方は落とさない", () => {
      // フェーズと指差喚呼は試合の進行そのものを握っている。コートが読めない
      // ことを理由にそちらまで捨てるほうがはるかに悪い
      expect(matchStateOf({ ...base, court: "green", phase: "match" }).phase).toBe("match");
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
          last_error: null,
          // **空配列へ倒さない。** 空は「除外なし = 全ステップが登録されている」を
          // 既に意味するので、読めなかった配信をそこへ埋めると、除外が起きているのに
          // 画面が平常を描く (除外を黙って行うのと同じ壊れ方)
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

    it("steps が配列でなければ空配列にする", () => {
      const message = parse({ type: "motor_check_state", steps: "壊れた値" });

      expect(message?.type).toBe("motor_check_state");
      if (message?.type !== "motor_check_state") return;
      expect(message.motorCheck.steps).toEqual([]);
    });

    it("除外したステップと欠けている軸をそのまま運ぶ", () => {
      // **除外が受信境界で消えると、動作確認そのものが意味を失う。**
      // サブハンド不在で減っているのか config の書き忘れで減っているのかを、
      // 操縦者はこれ以外に区別する材料を持たない
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
      // 空配列 (= 除外なし) へ倒すと、読めなかった配信が「全ステップ登録済み」に化ける
      const message = parse({
        type: "motor_check_state",
        excluded_steps: [{ step: "サブハンド 昇降" }],
      });

      expect(message?.type).toBe("motor_check_state");
      if (message?.type !== "motor_check_state") return;
      expect(message.motorCheck.excluded_steps).toBe(MALFORMED);
    });
  });

  describe("tuning_capture", () => {
    /** 実配信と同じく全欄が揃った指標。半端な形は受信境界で MALFORMED になる */
    const METRICS = {
      step_from: 0,
      step_to: 10,
      step_size: 10,
      rise_time_s: 0.05,
      overshoot_pct: 12,
      peak_time_s: 0.08,
      settling_time_s: null,
      steady_state_error: 0.1,
      oscillation_hz: null,
      damping_ratio: null,
      saturation_ratio: 0.2,
      peak_output: 900,
      settle_band: 1,
      sample_count: 8,
      duration_s: 0.14,
    };

    // eslint の consistent-function-scoping はここを外へ出せと言うが、payload は
    // tuning_capture 用の組み立てで、この describe の外で使う場面が無い
    // oxlint-disable-next-line unicorn/consistent-function-scoping
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
          metrics: METRICS,
          advice: [{ code: "overshoot", severity: "info", message: "行き過ぎ" }],
        }),
      );

      expect(message?.type).toBe("tuning_capture");
      if (message?.type !== "tuning_capture") return;
      expect(message.capture.samples.pos).toEqual([0, 9]);
      expect(message.capture.metrics).toEqual(METRICS);
      expect(message.capture.advice).toHaveLength(1);
    });

    it.each(["overshoot_pct", "settle_band", "step_to", "duration_s"])(
      "%s が欠けた指標は MALFORMED (null へ倒さない)",
      (key) => {
        // null は「ステップとして解釈できなかった」の表現。混ぜると、配信側の
        // 不具合が「そういう記録もある」として画面から見えなくなる。
        // 半端な形をそのまま通していた頃は `m.overshoot_pct.toFixed(0)` が
        // MetricsPanel のレンダー本体で投げていた
        const broken: Record<string, unknown> = { ...METRICS };
        delete broken[key];
        const message = parse(payload({ metrics: broken }));

        expect(message?.type).toBe("tuning_capture");
        if (message?.type !== "tuning_capture") return;
        expect(message.capture.metrics).toBe(MALFORMED);
      },
    );

    it("測れなかった項目の null は通す (欠落と区別する)", () => {
      // rise_time_s の null は「窓の中で目標の 90% へ届かなかった」の意味で、
      // 正常な配信。ここを弾くと指標そのものが出なくなる
      const message = parse(payload({ metrics: { ...METRICS, rise_time_s: null } }));

      expect(message?.type).toBe("tuning_capture");
      if (message?.type !== "tuning_capture") return;
      expect(message.capture.metrics).toMatchObject({ rise_time_s: null });
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

/**
 * 測定値の読み取り。**「測る手段が無い (null)」と「配信が読めない (欠落・型違い)」を
 * 混ぜてはならない** —— 前者は `—` を描くのが正しく、後者は異常側へ倒す。
 * `motors` は受信境界で素通しなので (モータ名を UI へ書かない性質がそこで
 * 成立している)、両者を分ける唯一の入口がここになる。
 */
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
    // NaN / Infinity は toFixed が "NaN" や "Infinity" を描いてしまう
    expect(readMeasured(Number.NaN)).toBe(MALFORMED);
    expect(readMeasured(Number.POSITIVE_INFINITY)).toBe(MALFORMED);
  });
});

/**
 * 指令値の読み取り。`readMeasured` との違いは **未配信を異常にしない**ことだけ。
 * `command` は後から足された欄なので、配らない版のサーバーへ繋いだだけで
 * 全モータの POS 欄が `?` で埋まってはならない。
 */
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
