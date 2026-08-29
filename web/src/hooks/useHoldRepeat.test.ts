import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { HOLD_DELAY_MS, HOLD_INTERVAL_MS, useHoldRepeat } from "@/hooks/useHoldRepeat";

/**
 * ジョグの「押している間くり返す」制御。
 *
 * ここで守るのは **止まること**。1 つでも停止経路を取りこぼすと、指を離したのに
 * 機体が動き続ける。押し始めの発火より、停止経路の網羅のほうが重要。
 */
describe("useHoldRepeat", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  function setup(enabled = true) {
    const fire = vi.fn();
    const view = renderHook(({ on }) => useHoldRepeat(fire, on), {
      initialProps: { on: enabled },
    });
    return { fire, view };
  }

  it("押した瞬間に 1 回発火する", () => {
    const { fire, view } = setup();
    act(() => view.result.current.onPointerDown());
    // 単発の操作が待たされてはならない
    expect(fire).toHaveBeenCalledTimes(1);
  });

  it("待ち時間を過ぎると繰り返す", () => {
    const { fire, view } = setup();
    act(() => view.result.current.onPointerDown());
    act(() => vi.advanceTimersByTime(HOLD_DELAY_MS + HOLD_INTERVAL_MS * 3));
    expect(fire.mock.calls.length).toBeGreaterThan(3);
  });

  it("待ち時間の手前で離せば 1 回で終わる", () => {
    const { fire, view } = setup();
    act(() => view.result.current.onPointerDown());
    act(() => vi.advanceTimersByTime(HOLD_DELAY_MS - 50));
    act(() => view.result.current.onPointerUp());
    act(() => vi.advanceTimersByTime(1000));
    expect(fire).toHaveBeenCalledTimes(1);
  });

  // 停止経路は 1 つでも欠けると「離したのに動き続ける」になる
  it.each([
    ["onPointerUp", (h: ReturnType<typeof useHoldRepeat>) => h.onPointerUp()],
    ["onPointerLeave", (h: ReturnType<typeof useHoldRepeat>) => h.onPointerLeave()],
    ["onPointerCancel", (h: ReturnType<typeof useHoldRepeat>) => h.onPointerCancel()],
    ["onBlur", (h: ReturnType<typeof useHoldRepeat>) => h.onBlur()],
  ])("%s で連続発火が止まる", (_name, stop) => {
    const { fire, view } = setup();
    act(() => view.result.current.onPointerDown());
    act(() => vi.advanceTimersByTime(HOLD_DELAY_MS + HOLD_INTERVAL_MS * 2));
    const before = fire.mock.calls.length;

    act(() => stop(view.result.current));
    act(() => vi.advanceTimersByTime(HOLD_INTERVAL_MS * 10));

    expect(fire).toHaveBeenCalledTimes(before);
  });

  it("アンマウントでも止まる", () => {
    // タブを切り替えただけで送り続けないため
    const { fire, view } = setup();
    act(() => view.result.current.onPointerDown());
    act(() => vi.advanceTimersByTime(HOLD_DELAY_MS + HOLD_INTERVAL_MS));
    const before = fire.mock.calls.length;

    view.unmount();
    act(() => vi.advanceTimersByTime(HOLD_INTERVAL_MS * 10));

    expect(fire).toHaveBeenCalledTimes(before);
  });

  it("無効なら 1 回も発火しない", () => {
    const { fire, view } = setup(false);
    act(() => view.result.current.onPointerDown());
    act(() => vi.advanceTimersByTime(HOLD_DELAY_MS + HOLD_INTERVAL_MS * 5));
    expect(fire).not.toHaveBeenCalled();
  });

  it("押し直しても発火源が二重にならない", () => {
    // stop を挟まず start を 2 回呼ぶ経路 (連打) で interval が積み上がると、
    // 1 回の押下で 2 倍の速さのジョグが出る
    const { fire, view } = setup();
    act(() => view.result.current.onPointerDown());
    act(() => view.result.current.onPointerDown());
    fire.mockClear();
    act(() => vi.advanceTimersByTime(HOLD_DELAY_MS + HOLD_INTERVAL_MS * 4));
    // 4 間隔ぶん = 4 回。二重に走っていれば 8 回になる
    expect(fire).toHaveBeenCalledTimes(4);
  });
});
