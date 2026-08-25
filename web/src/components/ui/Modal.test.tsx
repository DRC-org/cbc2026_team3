import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { Modal } from "@/components/ui/Modal";

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

  /**
   * daisyUI の .modal-box は既定が opacity:0 / scale:.95 で、可視化するルールは
   * .modal-open 側にしかない。付け忘れると Tailwind のツリーシェイクでそのルールごと
   * CSS から消え、DOM には居るのに何も見えないモーダルが出荷される。
   */
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

    // 緊急停止オーバーレイは解除経路を Reset ボタンのみに限定する。
    // onClose を渡さなければ構造的に閉じられないことを保証する
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
});
