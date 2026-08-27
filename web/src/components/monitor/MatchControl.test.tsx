import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { MatchSettings } from "@/components/monitor/MatchControl";
import type { MatchPhase } from "@/lib/protocol";
import { DEFAULT_MATCH_STATE, renderWithRobot } from "@/test/robotContext";

function mount(phase: MatchPhase = "setup") {
  return renderWithRobot(<MatchSettings onRequestConfirm={vi.fn()} />, {
    matchState: { ...DEFAULT_MATCH_STATE, phase, court: "red" },
  });
}

/**
 * コート選択は「今どちらか」を色で示す唯一の場所 (誤ったコートのまま試合に入る事故は
 * 試合をそのまま落とす)。選択中の面はコートの色そのものでなければ意味を成さないため、
 * 汎用の反転表示 (`Button` の generic な selected) ではなく呼び出し側が色を持つ。
 */
describe("MatchSettings のコート選択", () => {
  it("選択中のコートを色付きの面と aria-pressed で示す", () => {
    mount();

    const red = screen.getByRole("button", { name: "赤コート" });
    const blue = screen.getByRole("button", { name: "青コート" });

    expect(red).toHaveAttribute("aria-pressed", "true");
    expect(red).toHaveClass("bg-error");
    expect(blue).toHaveAttribute("aria-pressed", "false");
    expect(blue).not.toHaveClass("bg-info");
  });

  it("選択されていないコートを押すと set_court を送る", async () => {
    const { context } = mount();

    await userEvent.click(screen.getByRole("button", { name: "青コート" }));

    expect(context.setCourt).toHaveBeenCalledWith("blue");
  });

  it("試合中はコートを変更させない (サーバーも同じフェーズで拒否する)", () => {
    mount("match");

    expect(screen.getByRole("button", { name: "赤コート" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "青コート" })).toBeDisabled();
  });
});
