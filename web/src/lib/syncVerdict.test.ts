import { describe, expect, it } from "vitest";

import { evaluateSync } from "@/lib/syncVerdict";

describe("evaluateSync", () => {
  it("揃っている軸は警告しない (0 を欠落として捨てない)", () => {
    // 0 は JS では falsy。ここを `deviation ? ... : null` で書くと、
    // 最も健全な状態だけが「測れていない」と表示される
    const v = evaluateSync({ deviation: 0, sync_tolerance: 2 });
    expect(v.tone).toBe("success");
    expect(v.ratio).toBe(0);
    expect(v.alert).toBe(false);
  });

  it("許容差の 6 割で警告へ入る (超過時点では既に機体が止まっている)", () => {
    expect(evaluateSync({ deviation: 1.19, sync_tolerance: 2 }).tone).toBe("success");
    expect(evaluateSync({ deviation: 1.2, sync_tolerance: 2 }).tone).toBe("warning");
    expect(evaluateSync({ deviation: 1.2, sync_tolerance: 2 }).alert).toBe(true);
  });

  it("許容差を超えたら error (ちょうどは超過ではない)", () => {
    expect(evaluateSync({ deviation: 2.5, sync_tolerance: 2 }).tone).toBe("error");
    expect(evaluateSync({ deviation: 2, sync_tolerance: 2 }).tone).toBe("warning");
  });

  it("負の偏差も絶対値で見る", () => {
    expect(evaluateSync({ deviation: -2.5, sync_tolerance: 2 }).tone).toBe("error");
  });

  it("しきい値が届いていなければ判定しない (既定値を捏造しない)", () => {
    const v = evaluateSync({ deviation: 5, sync_tolerance: null });
    expect(v.tone).toBe("neutral");
    expect(v.ratio).toBeNull();
    expect(v.alert).toBe(false);
  });

  it("ずれようのない軸・測れない軸は語らない", () => {
    expect(evaluateSync({ deviation: null, sync_tolerance: 2 }).tone).toBe("neutral");
    expect(evaluateSync({ deviation: null, sync_tolerance: null }).ratio).toBeNull();
  });
});
