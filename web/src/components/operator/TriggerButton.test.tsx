import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { TriggerButton } from "@/components/operator/TriggerButton";

const BASE = { waiting: false, stepIndex: 0, totalSteps: 5, onTrigger: () => {} };

describe("TriggerButton", () => {
  it("トリガー待ちでは NEXT を押せる", async () => {
    const onTrigger = vi.fn();
    render(<TriggerButton {...BASE} waiting onTrigger={onTrigger} />);

    const button = screen.getByRole("button", { name: "次のステップへ進む" });
    expect(button).toBeEnabled();
    expect(button).toHaveTextContent("NEXT");

    await userEvent.click(button);
    expect(onTrigger).toHaveBeenCalledTimes(1);
  });

  it("実行中は RUNNING を表示し押せない", () => {
    const onTrigger = vi.fn();
    render(<TriggerButton {...BASE} stepIndex={2} onTrigger={onTrigger} />);

    const button = screen.getByRole("button", { name: "シーケンス実行中" });
    expect(button).toBeDisabled();
    expect(button).toHaveTextContent("RUNNING");
  });

  it("試合中以外は理由付きで操作不可にする", () => {
    render(<TriggerButton {...BASE} waiting disabled disabledLabel="セッティング中" />);

    const button = screen.getByRole("button", { name: "操作不可: セッティング中" });
    expect(button).toBeDisabled();
    expect(button).toHaveTextContent("セッティング中");
  });

  it("disabled は waiting より優先される (押せてしまうと誤操作になるため)", () => {
    render(<TriggerButton {...BASE} waiting disabled />);

    expect(screen.queryByRole("button", { name: "次のステップへ進む" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /操作不可/ })).toBeDisabled();
  });

  describe("完走判定", () => {
    it("step_index が総ステップ数に達したら DONE", () => {
      render(<TriggerButton {...BASE} stepIndex={5} totalSteps={5} />);

      const button = screen.getByRole("button", { name: "シーケンス完走" });
      expect(button).toHaveTextContent("DONE");
      expect(button).toBeDisabled();
    });

    it("最終ステップ実行中 (index = total - 1) はまだ DONE にしない", () => {
      render(<TriggerButton {...BASE} stepIndex={4} totalSteps={5} />);
      expect(screen.getByRole("button", { name: "シーケンス実行中" })).toBeInTheDocument();
    });

    it("トリガー待ちの間は DONE にせず NEXT を出す", () => {
      render(<TriggerButton {...BASE} waiting stepIndex={5} totalSteps={5} />);
      expect(screen.getByRole("button", { name: "次のステップへ進む" })).toBeInTheDocument();
    });

    it("シーケンス未取得 (総ステップ数 0) では DONE にしない", () => {
      render(<TriggerButton {...BASE} stepIndex={0} totalSteps={0} />);
      expect(screen.getByRole("button", { name: "シーケンス実行中" })).toBeInTheDocument();
    });
  });
});
