import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { TriggerButton } from "@/components/operator/TriggerButton";

describe("TriggerButton", () => {
  it("トリガー待ちでは NEXT を押せる", async () => {
    const onTrigger = vi.fn();
    render(<TriggerButton kind="waiting_trigger" onTrigger={onTrigger} />);

    const button = screen.getByRole("button", { name: "次のステップへ進む" });
    expect(button).toBeEnabled();
    expect(button).toHaveTextContent("NEXT");

    await userEvent.click(button);
    expect(onTrigger).toHaveBeenCalledTimes(1);
  });

  it("実行中は RUNNING を表示し押せない", () => {
    render(<TriggerButton kind="running" onTrigger={vi.fn()} />);

    const button = screen.getByRole("button", { name: "シーケンス実行中" });
    expect(button).toBeDisabled();
    expect(button).toHaveTextContent("RUNNING");
  });

  it("完走は DONE", () => {
    render(<TriggerButton kind="complete" onTrigger={vi.fn()} />);

    const button = screen.getByRole("button", { name: "シーケンス完走" });
    expect(button).toHaveTextContent("DONE");
    expect(button).toBeDisabled();
  });

  it("シーケンス未取得は RUNNING でも DONE でもなく、そのまま伝える", () => {
    // 最後の return へ落として RUNNING を出していた頃は、同じ画面の状態表示が
    // 「待機中 — START で開始」で、ボタンだけが実行中を主張していた
    render(<TriggerButton kind="no_sequence" onTrigger={vi.fn()} />);

    const button = screen.getByRole("button", { name: "操作不可: シーケンス未取得" });
    expect(button).toBeDisabled();
    expect(button).toHaveTextContent("シーケンス未取得");
    expect(screen.queryByText("RUNNING")).not.toBeInTheDocument();
    expect(screen.queryByText("DONE")).not.toBeInTheDocument();
  });

  it("試合中以外は理由付きで操作不可にする", () => {
    render(
      <TriggerButton
        kind="waiting_trigger"
        onTrigger={vi.fn()}
        disabled
        disabledLabel="セッティング中"
      />,
    );

    const button = screen.getByRole("button", { name: "操作不可: セッティング中" });
    expect(button).toBeDisabled();
    expect(button).toHaveTextContent("セッティング中");
  });

  it("disabled は待機状態より優先される (押せてしまうと誤操作になるため)", () => {
    render(<TriggerButton kind="waiting_trigger" onTrigger={vi.fn()} disabled />);

    expect(screen.queryByRole("button", { name: "次のステップへ進む" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /操作不可/ })).toBeDisabled();
  });
});
