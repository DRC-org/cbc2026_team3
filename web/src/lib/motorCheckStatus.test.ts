import { describe, expect, it } from "vitest";

import { motorCheckStatus } from "@/lib/motorCheckStatus";
import type { MotorCheckSnapshot } from "@/lib/protocol";

function snapshot(over: Partial<MotorCheckSnapshot> = {}): MotorCheckSnapshot {
  return {
    available: true,
    blocked_reason: null,
    running: false,
    current_step: null,
    step_index: 0,
    total_steps: 3,
    steps: [],
    error: null,
    last_error: null,
    excluded_steps: [],
    ...over,
  };
}

describe("motorCheckStatus", () => {
  it("未実行を完了と読まない (ステップ表が届いているだけ)", () => {
    const status = motorCheckStatus(snapshot({ running: false, step_index: 0 }), true);

    expect(status.outcome).toBe("idle");
    expect(status.completedSteps).toBe(0);
  });

  it("実行中は進んだぶんだけ数える", () => {
    const status = motorCheckStatus(snapshot({ running: true, step_index: 2 }), true);

    expect(status.outcome).toBe("running");
    expect(status.completedSteps).toBe(2);
  });

  it("最後まで進んで初めて完了", () => {
    const status = motorCheckStatus(snapshot({ step_index: 3 }), true);

    expect(status.outcome).toBe("done");
    expect(status.completedSteps).toBe(3);
  });

  it("エラーは完了より優先する (途中まで進んでいても完了ではない)", () => {
    const status = motorCheckStatus(snapshot({ step_index: 3, error: "同期ずれ" }), true);

    expect(status.outcome).toBe("failed");
    expect(status.completedSteps).toBe(3);
  });

  it("ステップ数 0 は未読込であって完了ではない", () => {
    expect(motorCheckStatus(snapshot({ total_steps: 0 }), true).outcome).toBe("idle");
  });

  describe("失敗理由の畳み込み", () => {
    const failure = {
      step_index: 2,
      step: "メインハンド y 軸",
      message: "偏差 3.1 > 許容 2.0",
      limit_related: false,
    };

    it("error が無くても last_error だけで失敗と読む", () => {
      const status = motorCheckStatus(snapshot({ step_index: 3, last_error: failure }), true);

      expect(status.outcome).toBe("failed");
      expect(status.failureReason).toBe("偏差 3.1 > 許容 2.0");
    });

    it("両方来たら error を優先する (ステップ名まで含んだ表示 1 行のため)", () => {
      const status = motorCheckStatus(
        snapshot({
          step_index: 3,
          error: "ステップ 'X' で失敗しました: 偏差",
          last_error: failure,
        }),
        true,
      );

      expect(status.failureReason).toBe("ステップ 'X' で失敗しました: 偏差");
    });

    it("平常時は null (出すものが無い)", () => {
      expect(motorCheckStatus(snapshot(), true).failureReason).toBeNull();
    });
  });

  describe("起動可否", () => {
    it("サーバーの blocked_reason をそのまま出す (導出し直さない)", () => {
      const status = motorCheckStatus(snapshot({ blocked_reason: "試合中は実行できません" }), true);
      expect(status.reasonLabel).toBe("試合中は実行できません");
    });

    it("切断中は画面側でしか分からないので画面が理由を付ける", () => {
      expect(motorCheckStatus(snapshot(), false).reasonLabel).toBe("切断中のため不可");
    });

    it("押せるときは null", () => {
      expect(motorCheckStatus(snapshot(), true).reasonLabel).toBeNull();
    });
  });
});
