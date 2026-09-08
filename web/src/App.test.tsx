import { afterEach, describe, expect, it, vi } from "vitest";

afterEach(() => {
  window.history.replaceState(null, "", "/");
  vi.doUnmock("react-router");
  vi.resetModules();
});

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
