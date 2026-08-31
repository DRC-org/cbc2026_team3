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
    ...over,
  };
}

describe("motorCheckStatus", () => {
  /**
   * **実配信のスナップショットがそのままこの形。** かつてパネルはこれを「完了」と
   * 読み、全ステップに緑の ✓ を付けていた。`config/checklist.yaml` の
   * 「アクチュエータ動作確認 完了」は、この誤表示のままチェックが付く。
   */
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
    // 止まった位置までは実際に通っている。0 に戻すと進捗の情報が消える
    expect(status.completedSteps).toBe(3);
  });

  it("ステップ数 0 は未読込であって完了ではない", () => {
    expect(motorCheckStatus(snapshot({ total_steps: 0 }), true).outcome).toBe("idle");
  });

  describe("起動可否", () => {
    it("サーバーの blocked_reason をそのまま出す (導出し直さない)", () => {
      const status = motorCheckStatus(snapshot({ blocked_reason: "試合中は実行できません" }), true);
      expect(status.reasonLabel).toBe("試合中は実行できません");
    });

    it("切断中は画面側でしか分からないので画面が理由を付ける", () => {
      // サーバーへ届かないので拒否も返らない。理由が無いと「押したのに何も起きない」
      expect(motorCheckStatus(snapshot(), false).reasonLabel).toBe("切断中のため不可");
    });

    it("押せるときは null", () => {
      expect(motorCheckStatus(snapshot(), true).reasonLabel).toBeNull();
    });
  });
});
