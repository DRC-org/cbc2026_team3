import { useCallback, useState } from "react";

import type { ResolvedWsUrl, WsUrlSource } from "@/lib/wsUrl";
import { clearStoredWsUrl, normalizeWsUrl, resolveWsUrl, storeWsUrl } from "@/lib/wsUrl";

export interface UseWsUrlReturn {
  wsUrl: string;
  wsUrlSource: WsUrlSource;
  setWsUrl: (input: string) => boolean;
  resetWsUrl: () => void;
}

export function useWsUrl(): UseWsUrlReturn {
  const [resolved, setResolved] = useState<ResolvedWsUrl>(resolveWsUrl);

  const setWsUrl = useCallback((input: string): boolean => {
    const normalized = normalizeWsUrl(input);
    if (!normalized) return false;
    storeWsUrl(normalized);
    setResolved({ url: normalized, source: "stored" });
    return true;
  }, []);

  const resetWsUrl = useCallback(() => {
    clearStoredWsUrl();
    setResolved(resolveWsUrl());
  }, []);

  return { wsUrl: resolved.url, wsUrlSource: resolved.source, setWsUrl, resetWsUrl };
}
