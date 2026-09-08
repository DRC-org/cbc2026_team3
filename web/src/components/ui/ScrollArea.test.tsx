import { fireEvent, render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ScrollArea } from "@/components/ui/ScrollArea";

// jsdom はレイアウトを持たないので、寸法は置いて作る
function stubScroll(
  el: Element,
  size: { clientHeight: number; scrollHeight: number; scrollTop: number },
) {
  for (const [key, value] of Object.entries(size)) {
    Object.defineProperty(el, key, { value, configurable: true });
  }
  fireEvent.scroll(el);
}

const above = (c: HTMLElement) => c.querySelector(".bg-linear-to-b");
const below = (c: HTMLElement) => c.querySelector(".bg-linear-to-t");

function mount() {
  const view = render(
    <ScrollArea>
      <p>中身</p>
    </ScrollArea>,
  );
  const body = view.container.querySelector(".scroll");
  if (!body) throw new Error("スクロール面が見つからない");
  return { ...view, body };
}

describe("ScrollArea", () => {
  it("下に続きがあるあいだだけ下端に合図を出す", () => {
    const { container, body } = mount();

    stubScroll(body, { clientHeight: 300, scrollHeight: 1200, scrollTop: 0 });
    expect(below(container)).not.toBeNull();
    expect(above(container)).toBeNull();

    stubScroll(body, { clientHeight: 300, scrollHeight: 1200, scrollTop: 900 });
    expect(below(container)).toBeNull();
  });

  it("上に続きがあるあいだだけ上端に合図を出す", () => {
    const { container, body } = mount();

    stubScroll(body, { clientHeight: 300, scrollHeight: 1200, scrollTop: 400 });
    expect(above(container)).not.toBeNull();

    stubScroll(body, { clientHeight: 300, scrollHeight: 1200, scrollTop: 0 });
    expect(above(container)).toBeNull();
  });

  it("溢れていなければ上下とも何も出さない", () => {
    const { container, body } = mount();

    stubScroll(body, { clientHeight: 300, scrollHeight: 300, scrollTop: 0 });

    expect(above(container)).toBeNull();
    expect(below(container)).toBeNull();
  });

  it("端の 1px の丸め誤差では出さない", () => {
    const { container, body } = mount();

    stubScroll(body, { clientHeight: 300, scrollHeight: 1200, scrollTop: 899.6 });
    expect(below(container)).toBeNull();

    stubScroll(body, { clientHeight: 300, scrollHeight: 1200, scrollTop: 0.6 });
    expect(above(container)).toBeNull();
  });

  it("合図は操作を吸わない (下に隠れた行を押せなくしない)", () => {
    const { container, body } = mount();
    stubScroll(body, { clientHeight: 300, scrollHeight: 1200, scrollTop: 400 });

    for (const el of [above(container), below(container)]) {
      expect(el).not.toBeNull();
      expect(el?.className).toContain("pointer-events-none");
      expect(el?.getAttribute("aria-hidden")).toBe("true");
    }
  });
});
