import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useHoldKey } from "@/hooks/useHoldKey";
import { HOLD_DELAY_MS, HOLD_INTERVAL_MS } from "@/hooks/useRepeatController";

/**
 * キーボードからのジョグ。
 *
 * `useHoldRepeat` と同じく、守るのは **止まること**。キーボードには
 * 「離したのに `keyup` が来ない」経路 (Alt+Tab・タブの背面化) があるぶん、
 * ポインタより停止の網を広く張る必要がある。
 */
function down(key: string, init: KeyboardEventInit = {}, target: EventTarget = window): boolean {
  const event = new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true, ...init });
  target.dispatchEvent(event);
  return event.defaultPrevented;
}

function up(key: string, target: EventTarget = window): void {
  target.dispatchEvent(new KeyboardEvent("keyup", { key, bubbles: true }));
}

describe("useHoldKey", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => {
    vi.useRealTimers();
    document.body.innerHTML = "";
  });

  function setup(enabled = true, maxMultiplier = 1) {
    const fire = vi.fn();
    const view = renderHook(({ on }) => useHoldKey("ArrowRight", fire, on, maxMultiplier), {
      initialProps: { on: enabled },
    });
    return { fire, view };
  }

  it("押した瞬間に 1 回発火し、押し続けると繰り返す", () => {
    const { fire } = setup();
    act(() => void down("ArrowRight"));
    expect(fire).toHaveBeenCalledTimes(1);

    act(() => vi.advanceTimersByTime(HOLD_DELAY_MS + HOLD_INTERVAL_MS * 3));
    expect(fire.mock.calls.length).toBeGreaterThan(3);
  });

  it("担当していないキーには反応しない", () => {
    const { fire } = setup();
    act(() => void down("ArrowLeft"));
    expect(fire).not.toHaveBeenCalled();
  });

  it("OS のキーリピートでは発火源を増やさない", () => {
    // 連続発火を駆動するのは自前のタイマー 1 本だけ。OS のリピートが重なると
    // 押しっぱなしのジョグが 2 倍の速さで出る
    const { fire } = setup();
    act(() => void down("ArrowRight"));
    act(() => void down("ArrowRight", { repeat: true }));
    fire.mockClear();
    act(() => vi.advanceTimersByTime(HOLD_DELAY_MS + HOLD_INTERVAL_MS * 4));
    expect(fire).toHaveBeenCalledTimes(4);
  });

  it("入力欄で打っている間は発火しない", () => {
    // 目標値を打ちながらの ← → はカーソル移動であって機体を動かす操作ではない
    const { fire } = setup();
    const input = document.createElement("input");
    document.body.appendChild(input);

    act(() => void down("ArrowRight", {}, input));

    expect(fire).not.toHaveBeenCalled();
  });

  it("修飾キー併用では発火しない", () => {
    const { fire } = setup();
    act(() => void down("ArrowRight", { ctrlKey: true }));
    expect(fire).not.toHaveBeenCalled();
  });

  it("担当キーの既定動作は止める (ページが横スクロールしない)", () => {
    setup();
    let prevented = false;
    act(() => {
      prevented = down("ArrowRight");
    });
    expect(prevented).toBe(true);
  });

  // 停止経路。1 つでも欠けると「離したのに動き続ける」になる
  it("keyup で止まる", () => {
    const { fire } = setup();
    act(() => void down("ArrowRight"));
    act(() => vi.advanceTimersByTime(HOLD_DELAY_MS + HOLD_INTERVAL_MS * 2));
    const before = fire.mock.calls.length;

    act(() => up("ArrowRight"));
    act(() => vi.advanceTimersByTime(HOLD_INTERVAL_MS * 10));

    expect(fire).toHaveBeenCalledTimes(before);
  });

  it("ウィンドウのフォーカスを失うと止まる (Alt+Tab で keyup が来ない)", () => {
    const { fire } = setup();
    act(() => void down("ArrowRight"));
    act(() => vi.advanceTimersByTime(HOLD_DELAY_MS + HOLD_INTERVAL_MS * 2));
    const before = fire.mock.calls.length;

    act(() => void window.dispatchEvent(new Event("blur")));
    act(() => vi.advanceTimersByTime(HOLD_INTERVAL_MS * 10));

    expect(fire).toHaveBeenCalledTimes(before);
  });

  it("タブが背面へ回ると止まる", () => {
    const { fire } = setup();
    act(() => void down("ArrowRight"));
    act(() => vi.advanceTimersByTime(HOLD_DELAY_MS + HOLD_INTERVAL_MS * 2));
    const before = fire.mock.calls.length;

    const spy = vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    act(() => void document.dispatchEvent(new Event("visibilitychange")));
    act(() => vi.advanceTimersByTime(HOLD_INTERVAL_MS * 10));

    expect(fire).toHaveBeenCalledTimes(before);
    spy.mockRestore();
  });

  it("押している最中に無効化されたら止まる", () => {
    // 緊急停止・切断・軸選択の移動・モード離脱
    const { fire, view } = setup(true);
    act(() => void down("ArrowRight"));
    act(() => vi.advanceTimersByTime(HOLD_DELAY_MS + HOLD_INTERVAL_MS * 2));
    const before = fire.mock.calls.length;

    view.rerender({ on: false });
    act(() => vi.advanceTimersByTime(HOLD_INTERVAL_MS * 10));

    expect(fire).toHaveBeenCalledTimes(before);
  });

  it("アンマウントでも止まる", () => {
    const { fire, view } = setup();
    act(() => void down("ArrowRight"));
    act(() => vi.advanceTimersByTime(HOLD_DELAY_MS + HOLD_INTERVAL_MS));
    const before = fire.mock.calls.length;

    view.unmount();
    act(() => vi.advanceTimersByTime(HOLD_INTERVAL_MS * 10));

    expect(fire).toHaveBeenCalledTimes(before);
  });

  it("無効なら 1 回も発火しない", () => {
    const { fire } = setup(false);
    act(() => void down("ArrowRight"));
    act(() => vi.advanceTimersByTime(HOLD_DELAY_MS + HOLD_INTERVAL_MS * 5));
    expect(fire).not.toHaveBeenCalled();
  });

  it("押し続けると実効量が伸びる", () => {
    const { fire } = setup(true, 8);
    act(() => void down("ArrowRight"));
    expect(fire).toHaveBeenLastCalledWith(1);

    act(() => vi.advanceTimersByTime(HOLD_DELAY_MS + HOLD_INTERVAL_MS * 12));
    expect(Math.max(...fire.mock.calls.map(([m]) => m as number))).toBeGreaterThan(1);
  });
});
