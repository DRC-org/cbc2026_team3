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

function segments() {
  return {
    manual: screen.getByRole("button", { name: "手動操縦" }),
    sequence: screen.getByRole("button", { name: "半自動" }),
  };
}

describe("ModeSwitch", () => {
  it("どちらのモードでも両方の選択肢を並べる", () => {
    for (const mode of ["sequence", "manual"] as const) {
      const { view } = renderSwitch(mode);
      const { manual, sequence } = segments();

      expect(manual).toBeInTheDocument();
      expect(sequence).toBeInTheDocument();

      view.unmount();
    }
  });

  it("選択中の側を aria-pressed と色付きの面で示す", () => {
    const { view } = renderSwitch("manual");
    expect(segments().manual).toHaveAttribute("aria-pressed", "true");
    expect(segments().manual).toHaveClass("bg-warning");
    expect(segments().sequence).toHaveAttribute("aria-pressed", "false");
    expect(segments().sequence).not.toHaveClass("bg-neutral");
    view.unmount();

    renderSwitch("sequence");
    expect(segments().sequence).toHaveAttribute("aria-pressed", "true");
    expect(segments().sequence).toHaveClass("bg-neutral");
    expect(segments().manual).toHaveAttribute("aria-pressed", "false");
    expect(segments().manual).not.toHaveClass("bg-warning");
  });

  it("選択中の側を押しても切り替えを送らない", async () => {
    const user = userEvent.setup();

    const a = renderSwitch("sequence");
    await user.click(segments().sequence);
    expect(a.onChange).not.toHaveBeenCalled();
    a.view.unmount();

    const b = renderSwitch("manual");
    await user.click(segments().manual);
    expect(b.onChange).not.toHaveBeenCalled();
  });

  it("選ばれていない側を押すとそのモードを返す", async () => {
    const user = userEvent.setup();

    const a = renderSwitch("sequence");
    await user.click(segments().manual);
    expect(a.onChange).toHaveBeenCalledWith("manual");
    a.view.unmount();

    const b = renderSwitch("manual");
    await user.click(segments().sequence);
    expect(b.onChange).toHaveBeenCalledWith("sequence");
  });

  it("切り替えられないときは理由を出して両方を塞ぐ", () => {
    renderSwitch("sequence", "切断中のため操作できません");

    expect(segments().manual).toBeDisabled();
    expect(segments().sequence).toBeDisabled();
    expect(screen.getByText("切断中のため操作できません")).toBeInTheDocument();
  });

  it("手動中も同じ理由で両方を塞ぐ", () => {
    renderSwitch("manual", "切断中のため操作できません");

    expect(segments().manual).toBeDisabled();
    expect(segments().sequence).toBeDisabled();
    expect(screen.getByText("切断中のため操作できません")).toBeInTheDocument();
  });

  it("手動中はシーケンスが止まっていることを書く", () => {
    const { view } = renderSwitch("manual");
    expect(screen.getByText(/シーケンスは停止しています/)).toBeInTheDocument();
    view.unmount();

    renderSwitch("sequence");
    expect(screen.queryByText(/シーケンスは停止しています/)).toBeNull();
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

  it("モードでも塞がれ状態でも選択は帯の先頭のまま (EMG STOP の真下に押せる要素を置かない)", () => {
    const cases: Array<[Parameters<typeof renderSwitch>[0], string | null]> = [
      ["sequence", null],
      ["manual", null],
      ["sequence", "切断中のため操作できません"],
      ["manual", "切断中のため操作できません"],
    ];

    for (const [mode, reason] of cases) {
      const { band, view } = renderSwitch(mode, reason);
      const group = screen.getByRole("group", { name: "操作モード" });

      expect(band.firstElementChild).toBe(group);
      expect(group).toContainElement(segments().manual);
      expect(group).toContainElement(segments().sequence);
      expect(group.className).not.toMatch(/ml-auto/);

      if (reason) {
        const text = screen.getByText(reason);
        expect(band.lastElementChild).toBe(text);
        expect(text).toHaveClass("ml-auto");
      }

      view.unmount();
    }
  });
});
