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

  /**
   * サーバーは同じ失敗を `error` (表示 1 行) と `last_error` (どのステップで
   * 失敗したか) の 2 欄で言う。片方だけを読むと、置き場所が変わった瞬間に失敗が
   * 「未実行」と同じ表示へ落ちる —— 動作確認が失敗しても画面が黙る、という
   * 元の壊れ方そのものになる。
   */
  describe("失敗理由の畳み込み", () => {
    const failure = { step_index: 2, step: "メインハンド y 軸", message: "偏差 3.1 > 許容 2.0" };

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
      // サーバーへ届かないので拒否も返らない。理由が無いと「押したのに何も起きない」
      expect(motorCheckStatus(snapshot(), false).reasonLabel).toBe("切断中のため不可");
    });

    it("押せるときは null", () => {
      expect(motorCheckStatus(snapshot(), true).reasonLabel).toBeNull();
    });
  });
});
