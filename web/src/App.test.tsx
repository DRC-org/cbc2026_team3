import { afterEach, describe, expect, it, vi } from "vitest";

afterEach(() => {
  window.history.replaceState(null, "", "/");
  vi.resetModules();
});

describe("App の起動時リダイレクト", () => {
  /**
   * createBrowserRouter は生成時点の location を読み取るため、旧ブックマークの
   * 読み替えはモジュール評価の順序に依存する。書き換えが router 生成より後ろに
   * ずれると、`#pid-tuning` で開いても Monitor が描画される。
   */
  it("旧ハッシュ URL はルーター生成より前にパスへ書き換わる", async () => {
    window.history.replaceState(null, "", "/#pid-tuning");
    vi.resetModules();

    const { App } = await import("@/App");
    const { routes } = await import("@/routes");

    expect(App).toBeTypeOf("function");
    expect(routes).toHaveLength(1);
    expect(window.location.pathname).toBe("/pid-tuning");
    expect(window.location.hash).toBe("");
  });

  it("パス指定済みの URL には介入しない", async () => {
    window.history.replaceState(null, "", "/sub-hand");
    vi.resetModules();

    await import("@/App");

    expect(window.location.pathname).toBe("/sub-hand");
  });
});
