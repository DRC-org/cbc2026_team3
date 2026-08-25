import { useCallback, useState } from "react";

import type { ResolvedWsUrl, WsUrlSource } from "@/lib/wsUrl";
import { clearStoredWsUrl, normalizeWsUrl, resolveWsUrl, storeWsUrl } from "@/lib/wsUrl";

export interface UseWsUrlReturn {
  wsUrl: string;
  wsUrlSource: WsUrlSource;
  /** 正規化して保存し、接続先を切り替える。入力が不正なら false を返して何もしない */
  setWsUrl: (input: string) => boolean;
  /** 保存済み設定を破棄し、クエリ / env / origin による既定へ戻す */
  resetWsUrl: () => void;
}

export function useWsUrl(): UseWsUrlReturn {
  const [resolved, setResolved] = useState<ResolvedWsUrl>(resolveWsUrl);

  const setWsUrl = useCallback((input: string): boolean => {
    const normalized = normalizeWsUrl(input);
    if (!normalized) return false;
    storeWsUrl(normalized);
    // ?ws= が付いていても、今入力した値を即座に有効にする。
    // 「保存したのに繋ぎ先が変わらない」状態を作らないため（次回リロードではクエリが再び勝つ）
    setResolved({ url: normalized, source: "stored" });
    return true;
  }, []);

  const resetWsUrl = useCallback(() => {
    clearStoredWsUrl();
    setResolved(resolveWsUrl());
  }, []);

  return { wsUrl: resolved.url, wsUrlSource: resolved.source, setWsUrl, resetWsUrl };
}
