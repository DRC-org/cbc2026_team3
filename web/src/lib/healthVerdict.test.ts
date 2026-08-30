import { describe, expect, it } from "vitest";

import {
  describeSafetyIssues,
  evaluateHealth,
  motorTempTone,
  summarizeMotors,
  tempThresholdsOf,
} from "@/lib/healthVerdict";
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

/** 配信されたしきい値。値そのものはサーバーの config が決めるので固定値で良い */
const THRESHOLDS = { warning: 65, critical: 80 };

function safety(over: Partial<SafetyState> = {}): SafetyState {
  return {
    sync_violations: [],
    loops_running: true,
    monitors_running: true,
    position_loops: [{ bus: "can_m3508", running: true, paused: false, sync_violations: [] }],
    sync_monitors: [{ axes: ["y_axis"], running: true, violated: [] }],
    refreshers_running: true,
    target_refreshers: [{ motors: ["gripper"], running: true, paused: false }],
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
    expect(evaluateHealth(undefined)).toEqual({ tone: "neutral", label: "ヘルス未取得" });
  });

  it("異常が無ければ success", () => {
    expect(evaluateHealth(health()).tone).toBe("success");
  });

  it("バス停止は error", () => {
    const verdict = evaluateHealth(health({ buses: [bus({ state: "down" })] }));
    expect(verdict.tone).toBe("error");
    expect(verdict.label).toMatch(/can_m3508/);
  });

  it("バス劣化 (degraded) は warning であって error ではない", () => {
    // タブの LED だけが degraded を error 扱いしていた
    expect(evaluateHealth(health({ buses: [bus({ state: "degraded" })] })).tone).toBe("warning");
  });

  it("モータ fault は error", () => {
    expect(evaluateHealth(health({ motors: [motorHealth({ state: "fault" })] })).tone).toBe(
      "error",
    );
  });

  /**
   * 高温はサーバーが config の `temp_warning_c` で `MotorHealth.state = warning` として
   * 既に配信している。UI が温度テレメトリから重ねて数えていた頃は、同じ 1 基が
   * 「異常 2 件」として出ていた (しかも UI の境界 60℃ はサーバーの 65℃ とずれていた)。
   */
  it("高温モータをサーバー判定と二重に数えない", () => {
    const verdict = evaluateHealth(health({ motors: [motorHealth({ state: "warning" })] }));
    expect(verdict.tone).toBe("warning");
    expect(verdict.label).toMatch(/要確認 1 件/);
  });

  /**
   * サーバー (`lib/server.py` の `_compute_health` / `_health_unknown`) は健全性を
   * 計算できなかったとき、意図的に overall=down・buses/motors 空・detail 付きを配信する。
   * 内訳が空だからと UI が「異常なし」を出すと、そのフェイルセーフを画面側が無効化して
   * しまう (誰も異常を検知できない状態が、最も安全に見える表示になる)。
   */
  describe("サーバーの総合判定", () => {
    it("内訳が空でも overall=down なら error に倒す", () => {
      const verdict = evaluateHealth(
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
      const verdict = evaluateHealth(
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
      const verdict = evaluateHealth(health({ overall: "down", buses: [], motors: [] }));
      expect(verdict.tone).toBe("error");
      expect(verdict.label).toMatch(/判定不能/);
    });

    it("内訳から理由を挙げられるならそちらを優先する (対処に直結する)", () => {
      const verdict = evaluateHealth(health({ overall: "down", buses: [bus({ state: "down" })] }));
      expect(verdict.label).toMatch(/can_m3508/);
    });

    it("内訳が空でも overall=degraded なら warning に倒す", () => {
      const verdict = evaluateHealth(health({ overall: "degraded", buses: [], motors: [] }));
      expect(verdict.tone).toBe("warning");
    });
  });

  describe("安全機構", () => {
    it("同期ずれラッチは error にし、どの軸かを出す", () => {
      // 緊急停止を解除してもこの軸は動かない。復旧手順の選択に直結する
      const verdict = evaluateHealth(health(), safety({ sync_violations: ["y_axis"] }));
      expect(verdict.tone).toBe("error");
      expect(verdict.label).toMatch(/y_axis/);
    });

    it("保護ループの停止は error", () => {
      // WS は繋がったままモータ状態も届き続けるので、配信を読まない限り誰も気付けない
      expect(evaluateHealth(health(), safety({ loops_running: false })).tone).toBe("error");
      expect(evaluateHealth(health(), safety({ monitors_running: false })).tone).toBe("error");
    });

    it("目標値再送の停止は error (ファーム側ウォッチドッグで generic が全停止する)", () => {
      // 20Hz の再送が途切れると 500ms 後にグリッパ・コンベア・壁が無反応になる。
      // 位置制御ループ・同期監視の停止と同格の異常として扱う
      expect(evaluateHealth(health(), safety({ refreshers_running: false })).tone).toBe("error");
    });

    it("safety が未受信でも判定は成立する", () => {
      expect(evaluateHealth(health(), undefined).tone).toBe("success");
    });

    it("同期ずれラッチはバス停止より先に主張する (復旧操作が別物のため)", () => {
      const verdict = evaluateHealth(
        health({ buses: [bus({ state: "down" })] }),
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

  it("止まっている目標値再送をモータ名付きで返す", () => {
    // どのアクチュエータが指令を失ったかが分からないと、操縦者は何を疑えばいいか決められない
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
    // 再送タスクの一覧そのものが取れていない場合でも、異常であることは伝える
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

  /**
   * UI 側のフォールバック値を持つと、それがサーバーの config とずれたまま
   * 効き続ける (二重管理そのもの)。しきい値が届いていない間は色を付けない。
   */
  it("しきい値が未取得なら温度に関わらず neutral (独自の既定値を持たない)", () => {
    expect(motorTempTone(0, null)).toBe("neutral");
    expect(motorTempTone(70, null)).toBe("neutral");
    expect(motorTempTone(999, null)).toBe("neutral");
  });
});

/**
 * 片方だけで判定すると「warning は出ないのに danger だけ出る」中途半端な色分けになり、
 * しきい値が届いていないことも画面から読み取れない。
 */
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

/**
 * サマリーが独自判定を持っていた頃、温度が正常なら FAULT のモータがあっても
 * 「All operational」を出していた。判定はここ 1 箇所だけが持つ。
 */
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
