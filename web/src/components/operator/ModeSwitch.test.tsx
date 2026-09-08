import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ModeSwitch } from "@/components/operator/ModeSwitch";

function renderSwitch(
  mode: "sequence" | "manual",
  blockedReason: string | null = null,
  totalSteps: number | null = null,
) {
  const onChange = vi.fn();
  const view = render(
    <ModeSwitch
      mode={mode}
      onChange={onChange}
      blockedReason={blockedReason}
      sequenceName="sub_hand"
      totalSteps={totalSteps}
    />,
  );
  const band = view.container.firstElementChild as HTMLElement;
  return { onChange, band, view };
}

function layoutClasses(el: HTMLElement): string[] {
  return Array.from(el.classList)
    .filter((name) => !name.startsWith("border-l-") && !name.startsWith("bg-"))
    .toSorted();
}

describe("ModeSwitch", () => {
  it("現在のモードを状態チップで示す", () => {
    renderSwitch("manual");
    expect(screen.getByText(/手動操縦中/)).toBeInTheDocument();
    expect(screen.queryByText("半自動")).toBeNull();
  });

  it("半自動中は行き先だけを差し出す (選択中のモードと同義の説明文を並べない)", () => {
    renderSwitch("sequence");
    expect(screen.getByText("半自動")).toBeInTheDocument();
    expect(screen.queryByText(/手動操縦中/)).toBeNull();
    expect(screen.queryByText("シーケンス制御中")).toBeNull();
  });

  it("押すと切り替え先のモードを返す", async () => {
    const user = userEvent.setup();
    const { onChange } = renderSwitch("sequence");

    await user.click(screen.getByRole("button", { name: /手動操縦へ/ }));

    expect(onChange).toHaveBeenCalledWith("manual");
  });

  it("手動中のボタンは半自動へ戻す", async () => {
    const user = userEvent.setup();
    const { onChange } = renderSwitch("manual");

    await user.click(screen.getByRole("button", { name: /半自動へ戻る/ }));

    expect(onChange).toHaveBeenCalledWith("sequence");
  });

  it("手動中は帯そのものを警告色にする", () => {
    const { band } = renderSwitch("manual");
    expect(band).toHaveClass("border-l-warning", "bg-warning/10");
  });

  it("半自動中の帯は色を持たない", () => {
    const { band } = renderSwitch("sequence");
    expect(band).toHaveClass("border-l-base-300", "bg-base-100");
    expect(band).not.toHaveClass("border-l-warning");
  });

  it("帯の高さはモードで変えない", () => {
    const a = renderSwitch("sequence");
    const b = renderSwitch("manual");
    expect(layoutClasses(a.band)).toEqual(layoutClasses(b.band));
  });

  it("塞がれていても帯の高さは変わらない", () => {
    const a = renderSwitch("sequence");
    const b = renderSwitch("sequence", "切断中のため操作できません");
    expect(layoutClasses(a.band)).toEqual(layoutClasses(b.band));
  });

  it("枠付きパネルではなく罫線 1 本の帯として描く", () => {
    const { band } = renderSwitch("sequence");
    expect(band).toHaveClass("border-b");
    expect(band.className).not.toMatch(/(^| )card( |$)/);
  });

  it("画面切替タブと同じ形にしない", () => {
    const { band } = renderSwitch("manual");
    expect(screen.queryAllByRole("tab")).toHaveLength(0);
    expect(band.querySelector(".tabs")).toBeNull();
  });

  it("切り替えられないときは理由を出してボタンを塞ぐ", () => {
    renderSwitch("sequence", "切断中のため操作できません");
    expect(screen.getByRole("button", { name: /手動操縦へ/ })).toBeDisabled();
    expect(screen.getByText("切断中のため操作できません")).toBeInTheDocument();
  });

  it("手動から戻る側も同じ理由で塞ぐ", () => {
    renderSwitch("manual", "切断中のため操作できません");
    expect(screen.getByRole("button", { name: /半自動へ戻る/ })).toBeDisabled();
  });

  it("シーケンス名を出す", () => {
    renderSwitch("sequence");
    expect(screen.getByText("sub_hand")).toBeInTheDocument();
  });

  it("総ステップ数は渡されたときだけ出す", () => {
    const { view } = renderSwitch("sequence", null, 22);
    expect(screen.getByText(/全 22 ステップ/)).toBeInTheDocument();
    view.unmount();

    renderSwitch("sequence", null, null);
    expect(screen.queryByText(/ステップ/)).toBeNull();
  });

  it("切替ボタンを帯の右端に置かない (EMG STOP の真下に押せる要素を置かない)", () => {
    const { band } = renderSwitch("sequence", "切断中のため操作できません");

    const button = screen.getByRole("button", { name: /手動操縦へ/ });
    expect(band.firstElementChild).toBe(button);
    expect(button.className).not.toMatch(/ml-auto/);

    const reason = screen.getByText("切断中のため操作できません");
    expect(band.lastElementChild).toBe(reason);
    expect(reason).toHaveClass("ml-auto");
  });

  it("モードでも塞がれ状態でも切替ボタンは帯の先頭のまま", () => {
    const cases: Array<[Parameters<typeof renderSwitch>[0], string | null]> = [
      ["sequence", null],
      ["manual", null],
      ["sequence", "切断中のため操作できません"],
      ["manual", "切断中のため操作できません"],
    ];

    for (const [mode, reason] of cases) {
      const { band, view } = renderSwitch(mode, reason);
      const first = band.firstElementChild as HTMLElement;

      expect(first.tagName).toBe("BUTTON");
      expect(first.textContent).toMatch(mode === "manual" ? /半自動へ戻る/ : /手動操縦へ/);

      view.unmount();
    }
  });
});
