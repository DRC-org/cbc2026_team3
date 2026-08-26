import { describe, expect, it } from "vitest";

import {
  countHotMotors,
  describeSafetyIssues,
  evaluateHealth,
  motorTempTone,
} from "@/lib/healthVerdict";
import type {
  BusHealth,
  HealthSnapshot,
  MotorHealth,
  MotorState,
  SafetyState,
} from "@/lib/protocol";
import { TEMP_DANGER, TEMP_WARNING } from "@/lib/robots";

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
  return { timestamp: 0, overall: "ok", buses: [bus()], motors: [motorHealth()], ...over };
}

function motor(temp: number): MotorState {
  return { pos: 0, vel: 0, torque: 0, temp };
}

function safety(over: Partial<SafetyState> = {}): SafetyState {
  return {
    sync_violations: [],
    loops_running: true,
    monitors_running: true,
    position_loops: [{ bus: "can_m3508", running: true, paused: false, sync_violations: [] }],
    sync_monitors: [{ axes: ["y_axis"], running: true, violated: [] }],
    ...over,
  };
}

/**
 * 判定を 2 箇所に書くと「Monitor は READY と言うのに操縦者の画面は異常と言う」
 * 状態が生まれる。実際にタブの LED は degraded を error 扱いにしており、
 * 同じ瞬間に Monitor は黄「要確認」、タブは赤 LED を出していた。
 */
describe("evaluateHealth", () => {
  it("ヘルス未取得は neutral", () => {
    expect(evaluateHealth(undefined, {})).toEqual({ tone: "neutral", label: "ヘルス未取得" });
  });

  it("異常が無ければ success", () => {
    expect(evaluateHealth(health(), { y_axis_r: motor(30) }).tone).toBe("success");
  });

  it("バス停止は error", () => {
    const verdict = evaluateHealth(health({ buses: [bus({ state: "down" })] }), {});
    expect(verdict.tone).toBe("error");
    expect(verdict.label).toMatch(/can_m3508/);
  });

  it("バス劣化 (degraded) は warning であって error ではない", () => {
    // タブの LED だけが degraded を error 扱いしていた
    expect(evaluateHealth(health({ buses: [bus({ state: "degraded" })] }), {}).tone).toBe(
      "warning",
    );
  });

  it("モータ fault は error", () => {
    expect(evaluateHealth(health({ motors: [motorHealth({ state: "fault" })] }), {}).tone).toBe(
      "error",
    );
  });

  it("高温モータは件数に数えて warning", () => {
    const verdict = evaluateHealth(health(), { a: motor(TEMP_WARNING), b: motor(30) });
    expect(verdict.tone).toBe("warning");
    expect(verdict.label).toMatch(/1 件/);
  });

  describe("安全機構", () => {
    it("同期ずれラッチは error にし、どの軸かを出す", () => {
      // 緊急停止を解除してもこの軸は動かない。復旧手順の選択に直結する
      const verdict = evaluateHealth(health(), {}, safety({ sync_violations: ["y_axis"] }));
      expect(verdict.tone).toBe("error");
      expect(verdict.label).toMatch(/y_axis/);
    });

    it("保護ループの停止は error", () => {
      // WS は繋がったままモータ状態も届き続けるので、配信を読まない限り誰も気付けない
      expect(evaluateHealth(health(), {}, safety({ loops_running: false })).tone).toBe("error");
      expect(evaluateHealth(health(), {}, safety({ monitors_running: false })).tone).toBe("error");
    });

    it("safety が未受信でも判定は成立する", () => {
      expect(evaluateHealth(health(), {}, undefined).tone).toBe("success");
    });

    it("同期ずれラッチはバス停止より先に主張する (復旧操作が別物のため)", () => {
      const verdict = evaluateHealth(
        health({ buses: [bus({ state: "down" })] }),
        {},
        safety({ sync_violations: ["rotate"] }),
      );
      expect(verdict.label).toMatch(/rotate/);
    });
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

  it("動作確認中の一時停止 (paused) は異常として扱わない", () => {
    const issues = describeSafetyIssues(
      safety({
        position_loops: [{ bus: "can_m3508", running: true, paused: true, sync_violations: [] }],
      }),
    );
    expect(issues).toEqual([]);
  });
});

describe("motorTempTone", () => {
  it("しきい値でトーンが上がる", () => {
    expect(motorTempTone(TEMP_WARNING - 1)).toBe("success");
    expect(motorTempTone(TEMP_WARNING)).toBe("warning");
    expect(motorTempTone(TEMP_DANGER)).toBe("error");
  });

  it("温度を返さないモータは neutral", () => {
    expect(motorTempTone(null)).toBe("neutral");
  });
});

describe("countHotMotors", () => {
  it("警告温度以上の基数を数える", () => {
    expect(countHotMotors({ a: motor(TEMP_WARNING), b: motor(0), c: motor(TEMP_DANGER) })).toBe(2);
  });
});
