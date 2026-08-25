import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import { useWsUrl } from "@/hooks/useWsUrl";
import { WS_URL_STORAGE_KEY } from "@/lib/wsUrl";

beforeEach(() => {
  localStorage.clear();
});

describe("useWsUrl", () => {
  it("既定はページ origin 由来", () => {
    const { result } = renderHook(() => useWsUrl());
    expect(result.current.wsUrl).toBe(`ws://${window.location.host}/ws`);
    expect(result.current.wsUrlSource).toBe("origin");
  });

  it("保存済み設定があれば起動時から使う", () => {
    localStorage.setItem(WS_URL_STORAGE_KEY, "ws://drc:8080/ws");
    const { result } = renderHook(() => useWsUrl());
    expect(result.current.wsUrl).toBe("ws://drc:8080/ws");
    expect(result.current.wsUrlSource).toBe("stored");
  });

  it("省略形の入力を正規化して保存し、即座に反映する", () => {
    const { result } = renderHook(() => useWsUrl());

    act(() => {
      expect(result.current.setWsUrl("drc:8080")).toBe(true);
    });

    expect(result.current.wsUrl).toBe("ws://drc:8080/ws");
    expect(result.current.wsUrlSource).toBe("stored");
    expect(localStorage.getItem(WS_URL_STORAGE_KEY)).toBe("ws://drc:8080/ws");
  });

  it("不正な入力では false を返し、接続先を変えない", () => {
    const { result } = renderHook(() => useWsUrl());
    const before = result.current.wsUrl;

    act(() => {
      expect(result.current.setWsUrl("  ")).toBe(false);
    });

    expect(result.current.wsUrl).toBe(before);
    expect(localStorage.getItem(WS_URL_STORAGE_KEY)).toBeNull();
  });

  it("リセットで保存を破棄し origin へ戻す", () => {
    localStorage.setItem(WS_URL_STORAGE_KEY, "ws://drc:8080/ws");
    const { result } = renderHook(() => useWsUrl());

    act(() => result.current.resetWsUrl());

    expect(result.current.wsUrl).toBe(`ws://${window.location.host}/ws`);
    expect(result.current.wsUrlSource).toBe("origin");
    expect(localStorage.getItem(WS_URL_STORAGE_KEY)).toBeNull();
  });
});
