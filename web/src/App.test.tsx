import { afterEach, describe, expect, it, vi } from "vitest";

afterEach(() => {
  window.history.replaceState(null, "", "/");
  vi.doUnmock("react-router");
  vi.resetModules();
});

/**
 * ルーター生成の瞬間に見えていた location を記録する。
 *
 * createBrowserRouter は生成時点の location しか読まないので、**書き換え後の
 * location を見るだけでは順序を確かめられない** —— 読み替えが生成より後ろへ
 * ずれても、テストの終わりには同じパスが立っている。
 */
function recordLocationAtRouterCreation(): string[] {
  const seen: string[] = [];
  vi.doMock("react-router", async () => {
    const actual = await vi.importActual<typeof import("react-router")>("react-router");
    return {
      ...actual,
      createBrowserRouter: (...args: Parameters<typeof actual.createBrowserRouter>) => {
        seen.push(window.location.pathname);
        return actual.createBrowserRouter(...args);
      },
    };
  });
  return seen;
}

describe("App の起動時リダイレクト", () => {
  /**
   * 各操縦者は担当タブの URL をブックマークして試合に臨む。書き換えが router 生成より
   * 後ろにずれると、`#main-hand` で開いても Monitor が描画される。
   */
  it("旧ハッシュ URL はルーター生成より前にパスへ書き換わる", async () => {
    window.history.replaceState(null, "", "/#main-hand");
    vi.resetModules();
    const seen = recordLocationAtRouterCreation();

    const { App } = await import("@/App");
    const { routes } = await import("@/routes");

    expect(App).toBeTypeOf("function");
    expect(routes).toHaveLength(1);
    expect(seen).toEqual(["/main-hand"]);
    expect(window.location.hash).toBe("");
  });

  it("パス指定済みの URL には介入しない", async () => {
    window.history.replaceState(null, "", "/sub-hand");
    vi.resetModules();
    const seen = recordLocationAtRouterCreation();

    await import("@/App");

    expect(seen).toEqual(["/sub-hand"]);
  });
});
