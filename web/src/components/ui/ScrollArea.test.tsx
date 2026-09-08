import { fireEvent, render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ScrollArea } from "@/components/ui/ScrollArea";

/**
 * 溢れが画面に出ない面は 1 つではない（指差喚呼 479px/1252px・機体状態 526px/1913px・
 * モータ一覧 24 基）。**部品の契約はここで固定し、呼び出し元のテストは
 * 「その面が実際にこれを使っているか」だけを見る。**
 *
 * 統合経路だけで確かめると、この判定を壊しても呼び出し元のどれか 1 つが拾って
 * 落ちるだけになり、「壊しても落ちない呼び出し元」が残る。
 */

/** jsdom はレイアウトを持たないので、溢れているかどうかは寸法を置いて作る */
function stubScroll(
  el: Element,
  size: { clientHeight: number; scrollHeight: number; scrollTop: number },
) {
  for (const [key, value] of Object.entries(size)) {
    Object.defineProperty(el, key, { value, configurable: true });
  }
  fireEvent.scroll(el);
}

/** 上端 / 下端の合図。クラスが唯一の手がかりなので直接引く */
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

    // 末尾まで送れば消える。出したままにすると「まだ続きがある」の意味が消え、
    // 項目の少ない構成では終わりを一度も読めなくなる
    stubScroll(body, { clientHeight: 300, scrollHeight: 1200, scrollTop: 900 });
    expect(below(container)).toBeNull();
  });

  it("上に続きがあるあいだだけ上端に合図を出す", () => {
    const { container, body } = mount();

    // 自動スクロール（指差喚呼の「次」・シーケンスの現在ステップ）は操縦者が
    // 動かさなくても起きるので、上端で切れた行が「描画の崩れ」に見えるのはこの状態
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
    // 実際の要素の寸法は小数を持つ。余裕が無いと、末尾まで送っても
    // 下端の合図が出たままになり「まだ続きがある」の意味が消える
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
      // 読み上げには出さない。中身は上下の要素そのもので、合図は形だけ
      expect(el?.getAttribute("aria-hidden")).toBe("true");
    }
  });
});
