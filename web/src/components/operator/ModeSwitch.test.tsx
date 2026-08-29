import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ModeSwitch } from "@/components/operator/ModeSwitch";

function renderSwitch(mode: "sequence" | "manual", blockedReason: string | null = null) {
  const onChange = vi.fn();
  render(<ModeSwitch mode={mode} onChange={onChange} blockedReason={blockedReason} />);
  return { onChange };
}

describe("ModeSwitch", () => {
  it("現在のモードが選択状態で示される", () => {
    renderSwitch("manual");
    expect(screen.getByRole("tab", { name: "手動操縦へ切り替え" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByRole("tab", { name: "半自動へ切り替え" })).toHaveAttribute(
      "aria-selected",
      "false",
    );
  });

  it("押すと切り替え先のモードを返す", async () => {
    const user = userEvent.setup();
    const { onChange } = renderSwitch("sequence");

    await user.click(screen.getByRole("tab", { name: "手動操縦へ切り替え" }));

    expect(onChange).toHaveBeenCalledWith("manual");
  });

  it("手動中は「シーケンスが停止している」ことを明示する", () => {
    // 機体を直接動かせる状態は、平常時と最も強く区別されるべき事実
    renderSwitch("manual");
    expect(screen.getByText(/手動操縦中/)).toBeInTheDocument();
  });

  it("半自動中はシーケンス制御中であることを示す", () => {
    renderSwitch("sequence");
    expect(screen.getByText("シーケンス制御中")).toBeInTheDocument();
    expect(screen.queryByText(/手動操縦中/)).toBeNull();
  });

  it("切り替えられないときは理由を出して他方を塞ぐ", () => {
    renderSwitch("sequence", "切断中のため操作できません");
    expect(screen.getByRole("tab", { name: "手動操縦へ切り替え" })).toBeDisabled();
    expect(screen.getByText("切断中のため操作できません")).toBeInTheDocument();
  });

  it("塞がれていても現在のモードのタブは押せる", () => {
    // 押しても同じモードなので何も起きない。無効化すると「今どちらか」が
    // 無効表示に埋もれて読みにくくなる
    renderSwitch("manual", "緊急停止中は手動操縦できません");
    expect(screen.getByRole("tab", { name: "手動操縦へ切り替え" })).toBeEnabled();
  });

  it("daisyUI のタブは親クラスと状態クラスが対で付く", () => {
    // tab-active だけ、または tab だけだと選択中の見た目ごと消える
    renderSwitch("manual");
    const active = screen.getByRole("tab", { name: "手動操縦へ切り替え" });
    expect(active).toHaveClass("tab", "tab-active");
    expect(active.parentElement).toHaveClass("tabs", "tabs-box");
  });
});
