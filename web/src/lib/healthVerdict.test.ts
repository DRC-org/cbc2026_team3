import { describe, expect, it } from "vitest";

import {
  describeSafetyIssues,
  evaluateHealth,
  firmwareUnconfirmedMotors,
  motorTempTone,
  summarizeMotors,
  tempThresholdsOf,
  workpieceRiskBuses,
} from "@/lib/healthVerdict";
import { MALFORMED } from "@/lib/protocol";
import type { BusHealth, HealthSnapshot, MotorHealth, SafetyState } from "@/lib/protocol";
import { DEFAULT_SERVER_INFO } from "@/test/robotContext";

function bus(over: Partial<BusHealth> = {}): BusHealth {
  return {
    name: "can_m3508",
    channel: "can0",
    state: "ok",
    last_tx_at: null,
    last_rx_at: null,
    tx_error_count: 0,
    rx_error_count: 0,
    bus_off: false,
    rx_down: false,
    rx_down_episodes: 0,
    may_affect_workpiece: false,
    ...over,
  };
}

function motorHealth(over: Partial<MotorHealth> = {}): MotorHealth {
  return {
    name: "y_axis_r",
    bus: "can_m3508",
    state: "ok",
    last_feedback_at: null,
    feedback_age_ms: 0,
    temperature: 30,
    detail: null,
    ...over,
  };
}

function health(over: Partial<HealthSnapshot> = {}): HealthSnapshot {
  return {
    timestamp: 0,
    overall: "ok",
    buses: [bus()],
    motors: [motorHealth()],
    detail: null,
    ...over,
  };
}

function verdictWhenConnected(
  snapshot: Parameters<typeof evaluateHealth>[0],
  safetyPayload?: Parameters<typeof evaluateHealth>[1],
) {
  return evaluateHealth(snapshot, safetyPayload, true);
}

const THRESHOLDS = { warning: 65, critical: 80 };

function safety(over: Partial<SafetyState> = {}): SafetyState {
  return {
    sync_violations: [],
    unenergized_motors: [],
    firmware_unconfirmed_motors: [],
    failed_tasks: [],
    reenergizing: false,
    loops_running: true,
    monitors_running: true,
    limit_monitors_running: true,
    position_loops: [{ bus: "can_m3508", running: true, paused: false, sync_violations: [] }],
    sync_monitors: [{ axes: ["y_axis"], running: true, violated: [] }],
    limit_monitors: [{ axes: ["sub_y_axis"], running: true, stopped: [] }],
    refreshers_running: true,
    target_refreshers: [{ motors: ["gripper"], running: true, paused: false }],
    ...over,
  };
}

describe("evaluateHealth", () => {
  it("ヘルス未取得は neutral", () => {
    expect(verdictWhenConnected(undefined)).toEqual({ tone: "neutral", label: "ヘルス未取得" });
  });

  it("異常が無ければ success", () => {
    expect(verdictWhenConnected(health()).tone).toBe("success");
  });

  it("バス停止は error", () => {
    const verdict = verdictWhenConnected(health({ buses: [bus({ state: "down" })] }));
    expect(verdict.tone).toBe("error");
    expect(verdict.label).toMatch(/can_m3508/);
  });

  it("バス劣化 (degraded) は warning であって error ではない", () => {
    expect(verdictWhenConnected(health({ buses: [bus({ state: "degraded" })] })).tone).toBe(
      "warning",
    );
  });

  it("モータ fault は error", () => {
    expect(verdictWhenConnected(health({ motors: [motorHealth({ state: "fault" })] })).tone).toBe(
      "error",
    );
  });

  it("高温モータをサーバー判定と二重に数えない", () => {
    const verdict = verdictWhenConnected(health({ motors: [motorHealth({ state: "warning" })] }));
    expect(verdict.tone).toBe("warning");
    expect(verdict.label).toMatch(/要確認 1 件/);
  });

  describe("サーバーの総合判定", () => {
    it("内訳が空でも overall=down なら error に倒す", () => {
      const verdict = verdictWhenConnected(
        health({
          overall: "down",
          buses: [],
          motors: [],
          detail: "ヘルス計算に失敗しました: boom",
        }),
      );
      expect(verdict.tone).toBe("error");
    });

    it("判定不能の理由 (detail) を捨てない", () => {
      const verdict = verdictWhenConnected(
        health({
          overall: "down",
          buses: [],
          motors: [],
          detail: "ヘルス計算に失敗しました: boom",
        }),
      );
      expect(verdict.detail).toBe("ヘルス計算に失敗しました: boom");
    });

    it("detail が無くても判定不能であることは伝える", () => {
      const verdict = verdictWhenConnected(health({ overall: "down", buses: [], motors: [] }));
      expect(verdict.tone).toBe("error");
      expect(verdict.label).toMatch(/判定不能/);
    });

    it("内訳から理由を挙げられるならそちらを優先する (対処に直結する)", () => {
      const verdict = verdictWhenConnected(
        health({ overall: "down", buses: [bus({ state: "down" })] }),
      );
      expect(verdict.label).toMatch(/can_m3508/);
    });

    it("内訳が空でも overall=degraded なら warning に倒す", () => {
      const verdict = verdictWhenConnected(health({ overall: "degraded", buses: [], motors: [] }));
      expect(verdict.tone).toBe("warning");
    });
  });

  describe("安全機構", () => {
    it("同期ずれラッチは error にし、どの軸かを出す", () => {
      const verdict = verdictWhenConnected(health(), safety({ sync_violations: ["y_axis"] }));
      expect(verdict.tone).toBe("error");
      expect(verdict.label).toMatch(/y_axis/);
    });

    it("保護ループの停止は error", () => {
      expect(verdictWhenConnected(health(), safety({ loops_running: false })).tone).toBe("error");
      expect(verdictWhenConnected(health(), safety({ monitors_running: false })).tone).toBe(
        "error",
      );
      expect(verdictWhenConnected(health(), safety({ limit_monitors_running: false })).tone).toBe(
        "error",
      );
    });

    it("目標値再送の停止は error (ファーム側ウォッチドッグで generic が全停止する)", () => {
      expect(verdictWhenConnected(health(), safety({ refreshers_running: false })).tone).toBe(
        "error",
      );
    });

    it("safety が未受信でも判定は成立する", () => {
      expect(verdictWhenConnected(health(), undefined).tone).toBe("success");
    });

    it("同期ずれラッチはバス停止より先に主張する (復旧操作が別物のため)", () => {
      const verdict = verdictWhenConnected(
        health({ buses: [bus({ state: "down" })] }),
        safety({ sync_violations: ["rotate"] }),
      );
      expect(verdict.label).toMatch(/rotate/);
    });
  });

  describe("欠けたヘルス配信", () => {
    const drop = (key: keyof HealthSnapshot) => {
      const broken: Record<string, unknown> = { ...health() };
      delete broken[key];
      return broken as unknown as HealthSnapshot;
    };

    it.each(["buses", "motors", "overall"] as const)("%s が欠けても投げず判定不能", (key) => {
      const verdict = verdictWhenConnected(drop(key));

      expect(verdict.tone).toBe("error");
      expect(verdict.label).toBe("健全性 判定不能");
      expect(verdict.detail).toContain(key);
    });

    it("未知の state が載っていても判定不能へ倒す", () => {
      const broken = health();
      (broken.buses as unknown[])[0] = { name: "can_m3508", state: "exploded" };
      expect(verdictWhenConnected(broken).tone).toBe("error");
    });

    it("受信境界が MALFORMED を立てた配信も判定不能へ倒す", () => {
      const verdict = verdictWhenConnected(MALFORMED);
      expect(verdict.tone).toBe("error");
      expect(verdict.label).toBe("健全性 判定不能");
      expect(verdict.detail).toMatch(/CAN もモータも異常を検知できない/);
    });

    it("欄の欠落は「どの欄が」を言う (MALFORMED と言い分ける)", () => {
      const verdict = verdictWhenConnected(drop("buses"));
      expect(verdict.detail).not.toMatch(/CAN もモータも異常を検知できない/);
      expect(verdict.detail).toContain("buses");
    });

    it("未配信 (undefined) は判定不能にしない (ヘルス未取得)", () => {
      expect(verdictWhenConnected(undefined).label).toBe("ヘルス未取得");
    });
  });

  describe("切断中", () => {
    it("正常な配信が手元にあっても判定不能へ倒す", () => {
      const verdict = evaluateHealth(health(), safety(), false);
      expect(verdict.tone).toBe("neutral");
      expect(verdict.label).toMatch(/通信断/);
    });

    it("異常が残っていても凍った判定を出さない", () => {
      const verdict = evaluateHealth(health({ buses: [bus({ state: "down" })] }), safety(), false);
      expect(verdict.label).toMatch(/通信断/);
    });

    it("なぜ判定できないかを添える", () => {
      expect(evaluateHealth(health(), undefined, false).detail).toMatch(/切断/);
    });
  });
});

describe("workpieceRiskBuses", () => {
  it("平常時 (エピソード 0) は返さない", () => {
    const snap = health({ buses: [bus({ may_affect_workpiece: true, rx_down_episodes: 0 })] });
    expect(workpieceRiskBuses(snap)).toEqual([]);
  });

  it("ワーク落下に無関係なバスの途絶は返さない (can_dm3520 / can_m3508 相当)", () => {
    const snap = health({
      buses: [bus({ may_affect_workpiece: false, rx_down_episodes: 3 })],
    });
    expect(workpieceRiskBuses(snap)).toEqual([]);
  });

  it("on_off を持つバスの途絶エピソードを返す (can_generic 相当)", () => {
    const risky = bus({ name: "can_generic", may_affect_workpiece: true, rx_down_episodes: 2 });
    const snap = health({ buses: [bus(), risky] });
    expect(workpieceRiskBuses(snap)).toEqual([risky]);
  });

  it("復旧して state が ok に戻ってもエピソード数が残っていれば返し続ける", () => {
    const risky = bus({
      name: "can_generic",
      state: "ok",
      may_affect_workpiece: true,
      rx_down_episodes: 1,
    });
    expect(workpieceRiskBuses(health({ buses: [risky] }))).toEqual([risky]);
  });

  it("未配信・読めない配信は空 (evaluateHealth 側が判定不能を別に報告する)", () => {
    expect(workpieceRiskBuses(undefined)).toEqual([]);
    expect(workpieceRiskBuses(MALFORMED)).toEqual([]);
  });
});

describe("firmwareUnconfirmedMotors", () => {
  it("平常時は返さない", () => {
    expect(firmwareUnconfirmedMotors(safety({ firmware_unconfirmed_motors: [] }))).toEqual([]);
  });

  it("未受信のモータをそのまま返す", () => {
    expect(firmwareUnconfirmedMotors(safety({ firmware_unconfirmed_motors: ["gripper"] }))).toEqual(
      ["gripper"],
    );
  });

  it("未配信・読めない配信は空 (evaluateHealth 側が判定不能を別に報告する)", () => {
    expect(firmwareUnconfirmedMotors(undefined)).toEqual([]);
    expect(firmwareUnconfirmedMotors(MALFORMED)).toEqual([]);
  });

  it("欄が配列でなくても投げず空を返す (全画面を落とさない)", () => {
    const broken = { ...safety(), firmware_unconfirmed_motors: "gripper" };

    expect(firmwareUnconfirmedMotors(broken as unknown as SafetyState)).toEqual([]);
  });

  it("欄が欠けていても投げず空を返す", () => {
    const broken: Record<string, unknown> = { ...safety() };
    delete broken.firmware_unconfirmed_motors;

    expect(firmwareUnconfirmedMotors(broken as unknown as SafetyState)).toEqual([]);
  });

  it("describeSafetyIssues には現れない (tone を動かさない)", () => {
    const payload = safety({ firmware_unconfirmed_motors: ["gripper"] });
    expect(describeSafetyIssues(payload)).toEqual([]);
  });
});

describe("describeSafetyIssues", () => {
  it("平常時は 1 件も返さない (静かにする)", () => {
    expect(describeSafetyIssues(safety())).toEqual([]);
    expect(describeSafetyIssues(undefined)).toEqual([]);
  });

  it("ラッチ中の軸と復旧手順を返す", () => {
    const issues = describeSafetyIssues(safety({ sync_violations: ["y_axis", "rotate"] }));
    expect(issues).toHaveLength(1);
    expect(issues[0].detail).toMatch(/y_axis/);
    expect(issues[0].detail).toMatch(/rotate/);
    expect(issues[0].hint).toMatch(/解除/);
  });

  it("無励磁のまま残ったモータを名前付きで返す", () => {
    const issues = describeSafetyIssues(safety({ unenergized_motors: ["sub_lift"] }));
    expect(issues).toHaveLength(1);
    expect(issues[0].detail).toMatch(/sub_lift/);
    expect(issues[0].hint).toMatch(/励磁/);
  });

  it("issue は機械可読の kind を持つ (UI は表示文字列で分岐しない)", () => {
    const issues = describeSafetyIssues(
      safety({ sync_violations: ["rotate"], unenergized_motors: ["sub_lift"] }),
    );
    expect(issues.map((issue) => issue.kind)).toEqual(["sync_violation", "unenergized"]);
  });

  it("無励磁のモータがあると異常判定へ倒す", () => {
    const verdict = verdictWhenConnected(health(), safety({ unenergized_motors: ["sub_lift"] }));
    expect(verdict.tone).toBe("error");
  });

  it("止まっている保護ループをバス名付きで返す", () => {
    const issues = describeSafetyIssues(
      safety({
        loops_running: false,
        position_loops: [{ bus: "can_m3508", running: false, paused: false, sync_violations: [] }],
      }),
    );
    expect(issues.some((i) => i.detail.includes("can_m3508"))).toBe(true);
  });

  it("止まっている同期監視を軸名付きで返す", () => {
    const issues = describeSafetyIssues(
      safety({
        monitors_running: false,
        sync_monitors: [{ axes: ["y_axis"], running: false, violated: [] }],
      }),
    );
    expect(issues.some((i) => i.detail.includes("y_axis"))).toBe(true);
  });

  it("止まっている可動端監視を軸名付きで返す", () => {
    const issues = describeSafetyIssues(
      safety({
        limit_monitors_running: false,
        limit_monitors: [{ axes: ["sub_y_axis"], running: false, stopped: [] }],
      }),
    );
    expect(issues.some((i) => i.kind === "limit_monitors_stopped")).toBe(true);
    expect(issues.some((i) => i.detail.includes("sub_y_axis"))).toBe(true);
  });

  it("止まっている目標値再送をモータ名付きで返す", () => {
    const issues = describeSafetyIssues(
      safety({
        refreshers_running: false,
        target_refreshers: [{ motors: ["gripper", "conveyor"], running: false, paused: false }],
      }),
    );
    const refresher = issues.find((i) => i.detail.includes("gripper"));
    expect(refresher?.detail).toMatch(/conveyor/);
    expect(refresher?.hint).toMatch(/500ms/);
  });

  it("集約値と内訳が同時に異常でも 1 件にまとめる (同じ事実を 2 度描かない)", () => {
    const issues = describeSafetyIssues(
      safety({
        refreshers_running: false,
        target_refreshers: [{ motors: ["gripper"], running: false, paused: false }],
      }),
    );
    expect(issues).toHaveLength(1);
    expect(issues[0].detail).toBe("gripper");
  });

  it("内訳が挙がらなくても集約値が false なら黙らない", () => {
    const issues = describeSafetyIssues(
      safety({ refreshers_running: false, target_refreshers: [] }),
    );
    expect(issues).toHaveLength(1);
    expect(issues[0].detail).toBe("全モータ");
  });

  it("動作確認中の一時停止 (paused) は異常として扱わない", () => {
    const issues = describeSafetyIssues(
      safety({
        position_loops: [{ bus: "can_m3508", running: true, paused: true, sync_violations: [] }],
        target_refreshers: [{ motors: ["gripper"], running: true, paused: true }],
      }),
    );
    expect(issues).toEqual([]);
  });

  describe("欠けた配信", () => {
    const drop = (key: keyof SafetyState) => {
      const broken: Record<string, unknown> = { ...safety() };
      delete broken[key];
      return broken as unknown as SafetyState;
    };

    it.each([
      "sync_violations",
      "unenergized_motors",
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
    ] as const)("%s が欠けても投げず、判定不能を 1 件返す", (key) => {
      const issues = describeSafetyIssues(drop(key));

      expect(issues).toHaveLength(1);
      expect(issues[0].label).toBe("安全機構 判定不能");
      expect(issues[0].detail).toContain(key);
      expect(issues[0].hint.length).toBeGreaterThan(0);
    });

    it("周期タスクの要素が読めなくても投げない", () => {
      const broken = safety();
      (broken.sync_monitors as unknown[])[0] = { running: true };

      const issues = describeSafetyIssues(broken);
      expect(issues).toHaveLength(1);
      expect(issues[0].detail).toContain("sync_monitors");
    });

    it("受信境界が MALFORMED を立てた配信も判定不能へ倒す", () => {
      const issues = describeSafetyIssues(MALFORMED);
      expect(issues).toHaveLength(1);
      expect(issues[0].label).toBe("安全機構 判定不能");
    });

    it("判定不能は evaluateHealth でも error になる (平常へ倒さない)", () => {
      expect(verdictWhenConnected(health(), MALFORMED).tone).toBe("error");
      expect(verdictWhenConnected(health(), drop("sync_violations")).tone).toBe("error");
    });

    it("未配信 (undefined) は判定不能にしない", () => {
      expect(describeSafetyIssues(undefined)).toEqual([]);
    });
  });
});

describe("motorTempTone", () => {
  it("配信されたしきい値でトーンが上がる", () => {
    expect(motorTempTone(THRESHOLDS.warning - 1, THRESHOLDS)).toBe("success");
    expect(motorTempTone(THRESHOLDS.warning, THRESHOLDS)).toBe("warning");
    expect(motorTempTone(THRESHOLDS.critical, THRESHOLDS)).toBe("error");
  });

  it("温度を返さないモータは neutral", () => {
    expect(motorTempTone(null, THRESHOLDS)).toBe("neutral");
  });

  it("しきい値が未取得なら温度に関わらず neutral (独自の既定値を持たない)", () => {
    expect(motorTempTone(0, null)).toBe("neutral");
    expect(motorTempTone(70, null)).toBe("neutral");
    expect(motorTempTone(999, null)).toBe("neutral");
  });
});

describe("tempThresholdsOf", () => {
  it("2 値が揃っていれば server_info の値をそのまま使う", () => {
    expect(
      tempThresholdsOf({ ...DEFAULT_SERVER_INFO, temp_warning_c: 65, temp_critical_c: 80 }),
    ).toEqual({ warning: 65, critical: 80 });
  });

  it("片方でも欠けていたら null", () => {
    expect(
      tempThresholdsOf({ ...DEFAULT_SERVER_INFO, temp_warning_c: 65, temp_critical_c: null }),
    ).toBeNull();
    expect(
      tempThresholdsOf({ ...DEFAULT_SERVER_INFO, temp_warning_c: null, temp_critical_c: 80 }),
    ).toBeNull();
  });

  it("server_info 未受信なら null", () => {
    expect(tempThresholdsOf(undefined)).toBeNull();
  });
});

describe("summarizeMotors", () => {
  it("全て ok なら All operational", () => {
    expect(summarizeMotors([motorHealth(), motorHealth({ name: "y_axis_l" })])).toEqual({
      tone: "success",
      label: "All operational",
    });
  });

  it("fault が 1 件でもあれば error", () => {
    const verdict = summarizeMotors([motorHealth({ state: "fault" }), motorHealth({ name: "b" })]);
    expect(verdict.tone).toBe("error");
    expect(verdict.label).toBe("異常 1 件");
  });

  it("fault が無く stale / warning だけなら warning", () => {
    expect(summarizeMotors([motorHealth({ state: "stale" })]).tone).toBe("warning");
    expect(summarizeMotors([motorHealth({ state: "warning" })]).tone).toBe("warning");
  });

  it("件数は ok 以外の総数 (fault も stale もまとめて数える)", () => {
    const verdict = summarizeMotors([
      motorHealth({ name: "a", state: "fault" }),
      motorHealth({ name: "b", state: "stale" }),
      motorHealth({ name: "c" }),
    ]);
    expect(verdict.label).toBe("異常 2 件");
  });

  it("未配信・空配列は success へ倒さない (異常の有無が分からない)", () => {
    expect(summarizeMotors(undefined)).toEqual({ tone: "neutral", label: "ヘルス未取得" });
    expect(summarizeMotors([])).toEqual({ tone: "neutral", label: "ヘルス未取得" });
  });
});
