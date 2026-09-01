import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider, createMemoryRouter } from "react-router";
import { beforeEach, describe, expect, it } from "vitest";

import { routes } from "@/routes";
import { installMockWebSocket } from "@/test/mockWebSocket";

function renderAt(initialEntry: string) {
  const router = createMemoryRouter(routes, { initialEntries: [initialEntry] });
  render(<RouterProvider router={router} />);
  return router;
}

describe("ルーティング", () => {
  beforeEach(() => {
    installMockWebSocket();
  });

  it("各タブのパスが対応する画面を描画する", async () => {
    renderAt("/monitor");
    expect(await screen.findByText("試合準備")).toBeInTheDocument();

    renderAt("/main-hand");
    expect(await screen.findByText("メインハンド")).toBeInTheDocument();

    renderAt("/sub-hand");
    expect(await screen.findByText("サブハンド")).toBeInTheDocument();
  });

  it("ルートと未知のパスは Monitor へ寄せる", async () => {
    const root = renderAt("/");
    expect(root.state.location.pathname).toBe("/monitor");

    const unknown = renderAt("/no-such-tab");
    expect(unknown.state.location.pathname).toBe("/monitor");
  });

  it("タブ遷移で ?ws= の上書きを落とさない", async () => {
    // 接続先の上書きはロード時にしか読まれないため、search を捨てると
    // 「タブを切り替えてリロードしたら既定へ戻る」事故になる
    const router = renderAt("/monitor?ws=drc:8080");
    await userEvent.click(screen.getByRole("link", { name: /Main Hand/ }));

    expect(router.state.location.pathname).toBe("/main-hand");
    expect(router.state.location.search).toBe("?ws=drc:8080");
  });

  it("数字キーでタブを切り替える", async () => {
    const router = renderAt("/monitor?ws=drc:8080");
    await userEvent.keyboard("3");

    expect(router.state.location.pathname).toBe("/sub-hand");
    expect(router.state.location.search).toBe("?ws=drc:8080");
  });
});
