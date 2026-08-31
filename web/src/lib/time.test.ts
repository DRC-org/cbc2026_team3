import { describe, expect, it } from "vitest";

import { formatAge, formatClock } from "@/lib/time";

/**
 * 書式化は純関数なので表示部品から切り離しておく。以前 `formatAge` は
 * `components/diagnostics/HealthIndicator.tsx` の export で、兄弟の
 * `MotorStatus` がそこから import していた。表示部品が別の表示部品の
 * ユーティリティ置き場になると、片方を消したときに巻き添えで壊れる。
 */
describe("formatAge", () => {
  it("未取得 (null / undefined / NaN) はダッシュで表す", () => {
    expect(formatAge(null)).toBe("—");
    expect(formatAge(undefined)).toBe("—");
    expect(formatAge(Number.NaN)).toBe("—");
  });

  it("負値は時刻ずれとみなしダッシュで表す", () => {
    expect(formatAge(-1)).toBe("—");
  });

  it("1 秒未満はミリ秒で丸めて出す", () => {
    expect(formatAge(0)).toBe("0ms 前");
    expect(formatAge(123.4)).toBe("123ms 前");
    expect(formatAge(999)).toBe("999ms 前");
  });

  it("1 分未満は小数 1 桁の秒で出す", () => {
    expect(formatAge(1000)).toBe("1.0s 前");
    expect(formatAge(59_999)).toBe("60.0s 前");
  });

  it("1 時間未満は分、それ以上は時間で出す", () => {
    expect(formatAge(60_000)).toBe("1m 前");
    expect(formatAge(3_599_999)).toBe("59m 前");
    expect(formatAge(3_600_000)).toBe("1h 前");
    expect(formatAge(7_200_000)).toBe("2h 前");
  });
});

/**
 * 表示は 1 通りだけ。以前は pill / card / compact / bus-only の 4 variant を持ち、
 * 本番から呼ばれるのは bus-only だけで、残る 3 つはテストからしか到達しなかった。
 * 「使われていないのに緑のまま残るコード」は、読む人に選択肢があると誤解させる。
 */

describe("formatClock", () => {
  it("読めない値はダッシュで表す (1970-01-01 を出さない)", () => {
    // サーバーの time.time() は**エポック秒**。ms のつもりで Date へ渡すと
    // 常に 1970-01-01 が出る。読めない値は数字を出さない側へ倒す
    expect(formatClock(null)).toBe("—");
    expect(formatClock(undefined)).toBe("—");
    expect(formatClock(Number.NaN)).toBe("—");
    expect(formatClock(Number.POSITIVE_INFINITY)).toBe("—");
  });

  it("エポックミリ秒を時計表示にする", () => {
    expect(formatClock(Date.UTC(2026, 0, 1, 0, 0, 0))).toMatch(/^\d{1,2}:\d{2}:\d{2}$/);
  });
});
