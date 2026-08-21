import { renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { HotkeyMap } from "@/hooks/useHotkeys";
import { useHotkeys } from "@/hooks/useHotkeys";

/** window へ keydown を流し、preventDefault されたかを返す */
function press(key: string, init: KeyboardEventInit = {}, target: EventTarget = window): boolean {
  const event = new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true, ...init });
  target.dispatchEvent(event);
  return event.defaultPrevented;
}

function mount(map: HotkeyMap, enabled = true) {
  return renderHook(({ m, e }: { m: HotkeyMap; e: boolean }) => useHotkeys(m, e), {
    initialProps: { m: map, e: enabled },
  });
}

afterEach(() => {
  document.body.innerHTML = "";
});

describe("発火", () => {
  it("登録したキーでハンドラを呼ぶ", () => {
    const onSpace = vi.fn();
    mount({ " ": onSpace });

    press(" ");
    expect(onSpace).toHaveBeenCalledTimes(1);
  });

  it("発火したキーは preventDefault する (直前に押したボタンの再実行を防ぐため)", () => {
    mount({ " ": vi.fn() });
    expect(press(" ")).toBe(true);
  });

  it("未登録のキーでは preventDefault しない", () => {
    mount({ " ": vi.fn() });
    expect(press("a")).toBe(false);
  });

  it("複数のキーをそれぞれのハンドラへ振り分ける", () => {
    const one = vi.fn();
    const two = vi.fn();
    mount({ "1": one, "2": two });

    press("2");
    expect(one).not.toHaveBeenCalled();
    expect(two).toHaveBeenCalledTimes(1);
  });
});

describe("誤爆の抑止", () => {
  it.each([
    ["Ctrl", { ctrlKey: true }],
    ["Meta", { metaKey: true }],
    ["Alt", { altKey: true }],
  ])("%s 修飾キーとの同時押しでは発火しない", (_label, init) => {
    const handler = vi.fn();
    mount({ " ": handler });

    expect(press(" ", init)).toBe(false);
    expect(handler).not.toHaveBeenCalled();
  });

  it("押しっぱなしによるリピートでは発火しない (トリガー多重送信の防止)", () => {
    const handler = vi.fn();
    mount({ " ": handler });

    press(" ", { repeat: true });
    expect(handler).not.toHaveBeenCalled();
  });

  it.each(["INPUT", "TEXTAREA", "SELECT"])("%s へのキー入力では発火しない", (tag) => {
    const handler = vi.fn();
    mount({ " ": handler });

    const el = document.createElement(tag);
    document.body.appendChild(el);
    press(" ", {}, el);

    expect(handler).not.toHaveBeenCalled();
  });

  it("contentEditable な要素へのキー入力では発火しない", () => {
    const handler = vi.fn();
    mount({ " ": handler });

    const el = document.createElement("div");
    el.contentEditable = "true";
    // jsdom は contentEditable から isContentEditable を導出しないため明示する
    Object.defineProperty(el, "isContentEditable", { value: true });
    document.body.appendChild(el);
    press(" ", {}, el);

    expect(handler).not.toHaveBeenCalled();
  });

  it("モーダル表示中は発火しない (緊急停止オーバーレイの裏でシーケンスが進むのを防ぐ)", () => {
    const handler = vi.fn();
    mount({ " ": handler });

    const modal = document.createElement("div");
    modal.className = "tui-modal active";
    document.body.appendChild(modal);
    press(" ");
    expect(handler).not.toHaveBeenCalled();

    // 閉じた (active が外れた) モーダルは抑止しない
    modal.className = "tui-modal";
    press(" ");
    expect(handler).toHaveBeenCalledTimes(1);
  });
});

describe("有効・無効の切り替え", () => {
  it("enabled=false では登録しない", () => {
    const handler = vi.fn();
    mount({ " ": handler }, false);

    press(" ");
    expect(handler).not.toHaveBeenCalled();
  });

  it("enabled を切り替えると登録・解除される", () => {
    const handler = vi.fn();
    const { rerender } = mount({ " ": handler }, false);

    rerender({ m: { " ": handler }, e: true });
    press(" ");
    expect(handler).toHaveBeenCalledTimes(1);

    rerender({ m: { " ": handler }, e: false });
    press(" ");
    expect(handler).toHaveBeenCalledTimes(1);
  });

  it("アンマウントでリスナーを解除する", () => {
    const handler = vi.fn();
    const { unmount } = mount({ " ": handler });

    unmount();
    press(" ");
    expect(handler).not.toHaveBeenCalled();
  });

  it("再レンダーでハンドラを差し替えても最新のものが呼ばれる", () => {
    const first = vi.fn();
    const second = vi.fn();
    const { rerender } = mount({ " ": first });

    rerender({ m: { " ": second }, e: true });
    press(" ");

    expect(first).not.toHaveBeenCalled();
    expect(second).toHaveBeenCalledTimes(1);
  });
});
