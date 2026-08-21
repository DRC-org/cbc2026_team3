import { describe, expect, it } from "vitest";

import { cx } from "@/lib/cx";

describe("cx", () => {
  it("クラス名を空白区切りで連結する", () => {
    expect(cx("tui-window", "black-255")).toBe("tui-window black-255");
  });

  it("条件付きクラスの false/null/undefined を落とす", () => {
    expect(cx("base", false, null, undefined, "active")).toBe("base active");
  });

  it("空文字も落とし、余分な空白を作らない", () => {
    expect(cx("", "base", "")).toBe("base");
  });

  it("有効なクラスが無ければ空文字を返す", () => {
    expect(cx(false, undefined)).toBe("");
  });
});
