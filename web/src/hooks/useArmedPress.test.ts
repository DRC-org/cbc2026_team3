import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ARM_GUARD_MS, ARM_TIMEOUT_MS, useArmedPress } from "@/hooks/useArmedPress";

/**
 * 二度押しは確認ダイアログの代わりである。守るのは「1 回では実行されない」ことと、
 * 「1 回の物理的なダブルクリックが二度押しとして成立しない」ことの 2 つ。
 * どちらかが欠けると、ボタンは確認を取っているように見えて取っていない。
 */
describe("useArmedPress", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  function setup() {
    const fire = vi.fn();
    const view = renderHook(() => useArmedPress(fire));
    return { fire, view };
  }

  /** 不感時間を抜けて発火できる状態まで進める */
  function passGuard() {
    act(() => vi.advanceTimersByTime(ARM_GUARD_MS));
  }

  it("1 回目では実行せず、武装だけする", () => {
    const { fire, view } = setup();

    act(() => view.result.current.press());

    expect(fire).not.toHaveBeenCalled();
    expect(view.result.current.armed).toBe(true);
  });

  it("不感時間を過ぎた 2 回目で実行する", () => {
    const { fire, view } = setup();

    act(() => view.result.current.press());
    passGuard();
    act(() => view.result.current.press());

    expect(fire).toHaveBeenCalledTimes(1);
    // 実行したら未武装へ戻す。戻さないと次の 1 回でもう一度実行される
    expect(view.result.current.armed).toBe(false);
  });

  it("不感時間の内側で押しても実行しない（ダブルクリック 1 回を二度押しにしない）", () => {
    const { fire, view } = setup();

    act(() => view.result.current.press());
    act(() => vi.advanceTimersByTime(ARM_GUARD_MS - 1));
    act(() => view.result.current.press());

    expect(fire).not.toHaveBeenCalled();
    // 捨てるのは 2 回目だけで、武装は解かない（解くと連打で 1 回目に戻り続ける）
    expect(view.result.current.armed).toBe(true);
  });

  it("不感時間中に押しても、自動解除までの猶予は引き直さない", () => {
    const { fire, view } = setup();

    act(() => view.result.current.press());
    act(() => vi.advanceTimersByTime(ARM_GUARD_MS - 1));
    act(() => view.result.current.press());
    // 1 回目からの猶予で解除される。押し直しで延長されるなら、ここではまだ武装中
    act(() => vi.advanceTimersByTime(ARM_TIMEOUT_MS - (ARM_GUARD_MS - 1)));

    expect(view.result.current.armed).toBe(false);
    act(() => view.result.current.press());
    expect(fire).not.toHaveBeenCalled();
  });

  it("放置すると武装が解け、次の 1 回はまた 1 回目になる", () => {
    const { fire, view } = setup();

    act(() => view.result.current.press());
    act(() => vi.advanceTimersByTime(ARM_TIMEOUT_MS));
    expect(view.result.current.armed).toBe(false);

    act(() => view.result.current.press());
    expect(fire).not.toHaveBeenCalled();
  });

  it("自動解除の直前ならまだ実行できる", () => {
    const { fire, view } = setup();

    act(() => view.result.current.press());
    act(() => vi.advanceTimersByTime(ARM_TIMEOUT_MS - 1));
    act(() => view.result.current.press());

    expect(fire).toHaveBeenCalledTimes(1);
  });

  it("disarm すると、その後の 1 回は 1 回目として扱う", () => {
    const { fire, view } = setup();

    act(() => view.result.current.press());
    passGuard();
    act(() => view.result.current.disarm());
    expect(view.result.current.armed).toBe(false);

    act(() => view.result.current.press());
    expect(fire).not.toHaveBeenCalled();
  });

  it("実行後に残ったタイマーで武装が復活しない", () => {
    const { fire, view } = setup();

    act(() => view.result.current.press());
    passGuard();
    act(() => view.result.current.press());
    act(() => vi.advanceTimersByTime(ARM_TIMEOUT_MS * 2));

    expect(fire).toHaveBeenCalledTimes(1);
    expect(view.result.current.armed).toBe(false);
  });

  it("アンマウント後にタイマーが残らない", () => {
    const { fire, view } = setup();

    act(() => view.result.current.press());
    view.unmount();
    act(() => vi.advanceTimersByTime(ARM_TIMEOUT_MS * 2));

    expect(fire).not.toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("最新の fire を呼ぶ（武装中に呼び出し側が再描画されても古い関数を握らない）", () => {
    const first = vi.fn();
    const second = vi.fn();
    const view = renderHook(({ f }) => useArmedPress(f), { initialProps: { f: first } });

    act(() => view.result.current.press());
    passGuard();
    view.rerender({ f: second });
    act(() => view.result.current.press());

    expect(first).not.toHaveBeenCalled();
    expect(second).toHaveBeenCalledTimes(1);
  });
});
