import { describe, expect, it } from "vitest";

import { deviationOf } from "@/lib/pidTuning";
import { motorState } from "@/test/motorState";

/**
 * 偏差は調整画面で最も見たい量なので、**出せないときに 0 を返してはならない。**
 * 「目標に完璧に追従している」と「そもそも測れない・目標が無い」が同じ表示になる。
 */
describe("deviationOf", () => {
  it("目標と実測が揃っていれば差を返す", () => {
    expect(deviationOf(motorState({ target: 10, pos: 8.5 }))).toBe(1.5);
  });

  it("目標を持たないモータは null", () => {
    expect(deviationOf(motorState({ target: null, pos: 8.5 }))).toBeNull();
  });

  it("位置を測れないモータは null (0 として引き算しない)", () => {
    // 0 を代入すると偏差が目標値そのものになり、しかも測っていないことが
    // 画面から消える (自作モータドライバの DC 基板・電磁弁基板がこの形)
    expect(deviationOf(motorState({ target: 10, pos: null }))).toBeNull();
  });
});
