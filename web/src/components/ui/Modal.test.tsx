import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { Modal } from "@/components/ui/Modal";

function Harness({ open }: { open: boolean }) {
  return (
    <>
      <button type="button">呼び出し元</button>
      <Modal open={open} title="T">
        本文
      </Modal>
    </>
  );
}

function press(key: string) {
  window.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true }));
}

describe("Modal", () => {
  it("閉じている間は何も描画しない", () => {
    render(
      <Modal open={false} title="T">
        本文
      </Modal>,
    );
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("表示中の外枠に modal-open が付く（modal-box を可視化する唯一のルール）", () => {
    const { container } = render(
      <Modal open title="T">
        本文
      </Modal>,
    );
    const root = container.querySelector(".modal");
    expect(root).not.toBeNull();
    expect(root).toHaveClass("modal-open");
    expect(container.querySelector(".modal-box")).not.toBeNull();
  });

  describe("閉じる手段は onClose の有無だけで決まる", () => {
    it("onClose があれば Esc と背景クリックで閉じる", async () => {
      const onClose = vi.fn();
      const { container } = render(
        <Modal open onClose={onClose} title="T">
          本文
        </Modal>,
      );

      press("Escape");
      expect(onClose).toHaveBeenCalledTimes(1);

      await userEvent.click(container.querySelector(".modal")!);
      expect(onClose).toHaveBeenCalledTimes(2);
    });

    it("onClose が無ければ Esc でも背景クリックでも閉じない", async () => {
      const { container } = render(
        <Modal open title="T">
          本文
        </Modal>,
      );

      press("Escape");
      await userEvent.click(container.querySelector(".modal")!);

      expect(screen.getByRole("dialog")).toBeInTheDocument();
    });

    it("本文のクリックでは閉じない", async () => {
      const onClose = vi.fn();
      render(
        <Modal open onClose={onClose} title="T">
          本文
        </Modal>,
      );

      await userEvent.click(screen.getByText("本文"));
      expect(onClose).not.toHaveBeenCalled();
    });
  });

  describe("読み上げ名とフォーカス", () => {
    it("見出しが読み上げ名になる (アクセシブル名の無いモーダルを作らない)", () => {
      render(
        <Modal open title="アクチュエータ動作確認">
          本文
        </Modal>,
      );

      expect(screen.getByRole("dialog", { name: "アクチュエータ動作確認" })).toBeInTheDocument();
    });

    it("ariaLabel を渡したときだけそちらが勝つ", () => {
      render(
        <Modal open title="T" ariaLabel="別の名前">
          本文
        </Modal>,
      );

      expect(screen.getByRole("dialog", { name: "別の名前" })).toBeInTheDocument();
    });

    it("開いたら中へフォーカスが入る", () => {
      render(
        <Modal open title="T">
          <button type="button">中のボタン</button>
        </Modal>,
      );

      expect(document.activeElement).toBe(screen.getByRole("dialog"));
    });

    it("閉じたら直前の要素へフォーカスが戻る", () => {
      const { rerender } = render(<Harness open={false} />);
      const opener = screen.getByRole("button", { name: "呼び出し元" });
      opener.focus();

      rerender(<Harness open />);
      expect(document.activeElement).not.toBe(opener);

      rerender(<Harness open={false} />);
      expect(document.activeElement).toBe(opener);
    });

    it("onClose があるモーダルは Tab が背後へ抜けない", async () => {
      render(
        <>
          <button type="button">背後のボタン</button>
          <Modal open onClose={vi.fn()} title="T">
            <button type="button">中 A</button>
            <button type="button">中 B</button>
          </Modal>
        </>,
      );

      for (let i = 0; i < 5; i++) {
        await userEvent.tab();
        expect(screen.getByRole("button", { name: "背後のボタン" })).not.toBe(
          document.activeElement,
        );
      }
    });

    it("onClose の無いモーダルは Tab で抜けても閉じない", async () => {
      render(
        <>
          <button type="button">背後のボタン</button>
          <Modal open title="EMERGENCY STOP">
            <button type="button">Reset</button>
          </Modal>
        </>,
      );

      for (let i = 0; i < 5; i++) await userEvent.tab();

      expect(screen.getByRole("dialog")).toBeInTheDocument();
    });
  });
});
