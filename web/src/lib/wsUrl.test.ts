import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { LocationLike } from "@/lib/wsUrl";
import {
  WS_URL_STORAGE_KEY,
  clearStoredWsUrl,
  normalizeWsUrl,
  originWsUrl,
  readStoredWsUrl,
  resolveWsUrl,
  storeWsUrl,
} from "@/lib/wsUrl";

function loc(over: Partial<LocationLike> = {}): LocationLike {
  return { protocol: "http:", host: "drc:5173", search: "", ...over };
}

beforeEach(() => {
  localStorage.clear();
});

afterEach(() => {
  vi.unstubAllEnvs();
});

describe("originWsUrl", () => {
  it("ページの host をそのまま使って /ws を組み立てる", () => {
    expect(originWsUrl(loc())).toBe("ws://drc:5173/ws");
  });

  it("https で配信されていれば wss になる", () => {
    expect(originWsUrl(loc({ protocol: "https:", host: "ctrl.example.com" }))).toBe(
      "wss://ctrl.example.com/ws",
    );
  });
});

describe("normalizeWsUrl", () => {
  it("ws:// / wss:// はそのまま通す", () => {
    expect(normalizeWsUrl("ws://drc:8080/ws", loc())).toBe("ws://drc:8080/ws");
    expect(normalizeWsUrl("wss://drc:8080/ws", loc())).toBe("wss://drc:8080/ws");
  });

  it("ブラウザからコピーした http(s) URL を ws(s) に読み替える", () => {
    expect(normalizeWsUrl("http://drc:8080/ws", loc())).toBe("ws://drc:8080/ws");
    expect(normalizeWsUrl("https://drc/ws", loc())).toBe("wss://drc/ws");
  });

  it("スキーム無しの host:port はページの protocol で補完する", () => {
    expect(normalizeWsUrl("drc:8080", loc())).toBe("ws://drc:8080/ws");
    expect(normalizeWsUrl("100.64.0.1:8080", loc())).toBe("ws://100.64.0.1:8080/ws");
    expect(normalizeWsUrl("drc:8080", loc({ protocol: "https:" }))).toBe("wss://drc:8080/ws");
  });

  it("パス省略時は /ws を補い、明示されたパスは保つ", () => {
    expect(normalizeWsUrl("ws://drc:8080", loc())).toBe("ws://drc:8080/ws");
    expect(normalizeWsUrl("ws://drc:8080/", loc())).toBe("ws://drc:8080/ws");
    expect(normalizeWsUrl("ws://drc:8080/socket", loc())).toBe("ws://drc:8080/socket");
  });

  it("前後の空白を無視する", () => {
    expect(normalizeWsUrl("  ws://drc:8080/ws  ", loc())).toBe("ws://drc:8080/ws");
  });

  it("空文字・ホスト無し・非対応スキームは null", () => {
    expect(normalizeWsUrl("", loc())).toBeNull();
    expect(normalizeWsUrl("   ", loc())).toBeNull();
    expect(normalizeWsUrl("ws://", loc())).toBeNull();
    expect(normalizeWsUrl("ftp://drc:8080", loc())).toBeNull();
  });
});

describe("localStorage への保存", () => {
  it("保存した URL を読み戻せる", () => {
    storeWsUrl("ws://drc:8080/ws");
    expect(localStorage.getItem(WS_URL_STORAGE_KEY)).toBe("ws://drc:8080/ws");
    expect(readStoredWsUrl()).toBe("ws://drc:8080/ws");
  });

  it("未保存なら null", () => {
    expect(readStoredWsUrl()).toBeNull();
  });

  it("clear で消える", () => {
    storeWsUrl("ws://drc:8080/ws");
    clearStoredWsUrl();
    expect(readStoredWsUrl()).toBeNull();
  });

  it("localStorage が使えない環境でも例外を投げない", () => {
    // プライベートブラウジング等ではアクセス自体が throw する。
    // 接続先設定の保存に失敗しても UI 全体を落としてはならない
    const spy = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("QuotaExceededError");
    });
    expect(() => storeWsUrl("ws://drc:8080/ws")).not.toThrow();
    spy.mockRestore();
  });
});

describe("resolveWsUrl の優先順位", () => {
  it("何も無ければページ origin 由来", () => {
    expect(resolveWsUrl(loc())).toEqual({ url: "ws://drc:5173/ws", source: "origin" });
  });

  it("VITE_WS_URL は origin より優先する", () => {
    vi.stubEnv("VITE_WS_URL", "ws://built-in:8080/ws");
    expect(resolveWsUrl(loc())).toEqual({ url: "ws://built-in:8080/ws", source: "env" });
  });

  it("保存済み設定は env より優先する", () => {
    vi.stubEnv("VITE_WS_URL", "ws://built-in:8080/ws");
    storeWsUrl("ws://saved:8080/ws");
    expect(resolveWsUrl(loc())).toEqual({ url: "ws://saved:8080/ws", source: "stored" });
  });

  it("?ws= クエリが最優先（保存済み設定を一時的に上書きできる）", () => {
    storeWsUrl("ws://saved:8080/ws");
    expect(resolveWsUrl(loc({ search: "?ws=ws://query:8080/ws" }))).toEqual({
      url: "ws://query:8080/ws",
      source: "query",
    });
  });

  it("?ws= はスキーム省略でも受け付ける", () => {
    expect(resolveWsUrl(loc({ search: "?ws=drc:8080" }))).toEqual({
      url: "ws://drc:8080/ws",
      source: "query",
    });
  });

  it("不正な値は次の候補にフォールバックする", () => {
    storeWsUrl("ftp://broken");
    expect(resolveWsUrl(loc({ search: "?ws=" }))).toEqual({
      url: "ws://drc:5173/ws",
      source: "origin",
    });
  });
});
