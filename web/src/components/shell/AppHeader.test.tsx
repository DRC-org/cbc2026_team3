import { screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";

import { AppHeader } from "@/components/shell/AppHeader";
import type { MatchState } from "@/lib/protocol";
import { MALFORMED } from "@/lib/protocol";
import { DEFAULT_MATCH_STATE, renderWithRobot } from "@/test/robotContext";

function mount(over: Partial<MatchState> = {}) {
  return renderWithRobot(
    <MemoryRouter>
      <AppHeader />
    </MemoryRouter>,
    { matchState: { ...DEFAULT_MATCH_STATE, ...over } },
  );
}

/**
 * フェーズチップとコートチップは `Record` の索引で描く。未知の値が素通しで入ると
 * 索引が `undefined` になり、`StatusBadge` がクラス無し・文字無しで描かれて
 * **帯からチップごと消える**。コートは「誤設定のまま試合に入る事故を防ぐため
 * 常時表示している」要素なので、消えたことに誰も気付けない。
 */
describe("AppHeader のフェーズ・コート表示", () => {
  it("既知の値はそのまま出す", () => {
    mount({ phase: "match", court: "blue" });

    expect(screen.getByText("試合中")).toBeInTheDocument();
    expect(screen.getByText("青コート")).toBeInTheDocument();
  });

  it("読めなかったフェーズを空白にせず「不明」として出す", () => {
    mount({ phase: MALFORMED });

    expect(screen.getByText("フェーズ不明")).toBeInTheDocument();
  });

  it("読めなかったコートを空白にせず「不明」として出す", () => {
    mount({ court: MALFORMED });

    expect(screen.getByText("コート不明")).toBeInTheDocument();
  });

  it("緊急停止は何があっても押せる位置に残る", () => {
    // ヘッダーは RouteErrorBoundary の外。ここが描けなくなると止める手段が消える
    mount({ phase: MALFORMED, court: MALFORMED });

    expect(screen.getByRole("button", { name: "緊急停止" })).toBeInTheDocument();
  });
});
