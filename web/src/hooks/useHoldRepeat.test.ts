import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  HOLD_ACCEL_EVERY,
  HOLD_DELAY_MS,
  HOLD_INTERVAL_MS,
  useHoldRepeat,
} from "@/hooks/useHoldRepeat";

/**
 * ジョグの「押している間くり返す」制御。
 *
 * ここで守るのは **止まること**。1 つでも停止経路を取りこぼすと、指を離したのに
 * 機体が動き続ける。押し始めの発火より、停止経路の網羅のほうが重要。
 */
describe("useHoldRepeat", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  function setup(enabled = true, maxMultiplier = 1) {
    const fire = vi.fn();
    const view = renderHook(({ on }) => useHoldRepeat(fire, on, maxMultiplier), {
      initialProps: { on: enabled },
    });
    return { fire, view, handlers: () => view.result.current.handlers };
  }

  it("押した瞬間に 1 回発火する", () => {
    const { fire, handlers } = setup();
    act(() => handlers().onPointerDown());
    // 単発の操作が待たされてはならない
    expect(fire).toHaveBeenCalledTimes(1);
  });

  it("待ち時間を過ぎると繰り返す", () => {
    const { fire, handlers } = setup();
    act(() => handlers().onPointerDown());
    act(() => vi.advanceTimersByTime(HOLD_DELAY_MS + HOLD_INTERVAL_MS * 3));
    expect(fire.mock.calls.length).toBeGreaterThan(3);
  });

  it("待ち時間の手前で離せば 1 回で終わる", () => {
    const { fire, handlers } = setup();
    act(() => handlers().onPointerDown());
    act(() => vi.advanceTimersByTime(HOLD_DELAY_MS - 50));
    act(() => handlers().onPointerUp());
    act(() => vi.advanceTimersByTime(1000));
    expect(fire).toHaveBeenCalledTimes(1);
  });

  // 停止経路は 1 つでも欠けると「離したのに動き続ける」になる
  it.each(["onPointerUp", "onPointerLeave", "onPointerCancel", "onBlur"] as const)(
    "%s で連続発火が止まる",
    (name) => {
      const { fire, handlers } = setup();
      act(() => handlers().onPointerDown());
      act(() => vi.advanceTimersByTime(HOLD_DELAY_MS + HOLD_INTERVAL_MS * 2));
      const before = fire.mock.calls.length;

      act(() => handlers()[name]());
      act(() => vi.advanceTimersByTime(HOLD_INTERVAL_MS * 10));

      expect(fire).toHaveBeenCalledTimes(before);
    },
  );

  it("アンマウントでも止まる", () => {
    // タブを切り替えただけで送り続けないため
    const { fire, view, handlers } = setup();
    act(() => handlers().onPointerDown());
    act(() => vi.advanceTimersByTime(HOLD_DELAY_MS + HOLD_INTERVAL_MS));
    const before = fire.mock.calls.length;

    view.unmount();
    act(() => vi.advanceTimersByTime(HOLD_INTERVAL_MS * 10));

    expect(fire).toHaveBeenCalledTimes(before);
  });

  it("無効なら 1 回も発火しない", () => {
    const { fire, handlers } = setup(false);
    act(() => handlers().onPointerDown());
    act(() => vi.advanceTimersByTime(HOLD_DELAY_MS + HOLD_INTERVAL_MS * 5));
    expect(fire).not.toHaveBeenCalled();
  });

  it("押している最中に無効化されたら止まる", () => {
    // 緊急停止・切断・モード離脱。押し始めたときの可否のまま回り続けてはならない
    const { fire, view, handlers } = setup(true);
    act(() => handlers().onPointerDown());
    act(() => vi.advanceTimersByTime(HOLD_DELAY_MS + HOLD_INTERVAL_MS * 2));
    const before = fire.mock.calls.length;

    view.rerender({ on: false });
    act(() => vi.advanceTimersByTime(HOLD_INTERVAL_MS * 10));

    expect(fire).toHaveBeenCalledTimes(before);
  });

  it("押し直しても発火源が二重にならない", () => {
    // stop を挟まず start を 2 回呼ぶ経路 (連打) で interval が積み上がると、
    // 1 回の押下で 2 倍の速さのジョグが出る
    const { fire, handlers } = setup();
    act(() => handlers().onPointerDown());
    act(() => handlers().onPointerDown());
    fire.mockClear();
    act(() => vi.advanceTimersByTime(HOLD_DELAY_MS + HOLD_INTERVAL_MS * 4));
    // 4 間隔ぶん = 4 回。二重に走っていれば 8 回になる
    expect(fire).toHaveBeenCalledTimes(4);
  });

  describe("押し続けたときの加速", () => {
    it("最初の 1 回は必ず等倍で出る", () => {
      // 軽く 1 回押したつもりが大きく動いてはならない
      const { fire, handlers } = setup(true, 8);
      act(() => handlers().onPointerDown());
      expect(fire).toHaveBeenLastCalledWith(1);
    });

    it("一定回数ごとに実効量が倍になる", () => {
      const { fire, handlers } = setup(true, 8);
      act(() => handlers().onPointerDown());
      act(() => vi.advanceTimersByTime(HOLD_DELAY_MS + HOLD_INTERVAL_MS * HOLD_ACCEL_EVERY));

      expect(fire.mock.calls.slice(0, HOLD_ACCEL_EVERY).every(([m]) => m === 1)).toBe(true);
      expect(fire).toHaveBeenLastCalledWith(2);
    });

    it("上限を超えて伸びない", () => {
      // 1 回の押下で可動域を飛び越えさせない。上限は呼び出し側が可動範囲から決める
      const { fire, handlers } = setup(true, 2);
      act(() => handlers().onPointerDown());
      act(() => vi.advanceTimersByTime(HOLD_DELAY_MS + HOLD_INTERVAL_MS * HOLD_ACCEL_EVERY * 5));
      expect(Math.max(...fire.mock.calls.map(([m]) => m as number))).toBe(2);
    });

    it("既定では加速しない", () => {
      const { fire, handlers } = setup(true);
      act(() => handlers().onPointerDown());
      act(() => vi.advanceTimersByTime(HOLD_DELAY_MS + HOLD_INTERVAL_MS * HOLD_ACCEL_EVERY * 3));
      expect(fire.mock.calls.every(([m]) => m === 1)).toBe(true);
    });

    it("離すと倍率が 1 へ戻る", () => {
      // 前回の押下の勢いが残っていると、次の 1 押しの結果を予測できない
      const { fire, view, handlers } = setup(true, 8);
      act(() => handlers().onPointerDown());
      act(() => vi.advanceTimersByTime(HOLD_DELAY_MS + HOLD_INTERVAL_MS * HOLD_ACCEL_EVERY));
      expect(view.result.current.multiplier).toBe(2);

      act(() => handlers().onPointerUp());
      expect(view.result.current.multiplier).toBe(1);

      fire.mockClear();
      act(() => handlers().onPointerDown());
      expect(fire).toHaveBeenLastCalledWith(1);
    });
  });
});
