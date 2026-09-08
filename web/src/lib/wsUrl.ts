export type WsUrlSource = "query" | "stored" | "env" | "origin";

export interface ResolvedWsUrl {
  url: string;
  source: WsUrlSource;
}

export interface LocationLike {
  protocol: string;
  host: string;
  search: string;
}

export const WS_URL_STORAGE_KEY = "cbc2026.ws_url";
export const WS_URL_QUERY_KEY = "ws";

const DEFAULT_PATH = "/ws";
const SCHEME_RE = /^[a-z][a-z0-9+.-]*:\/\//i;

function currentLocation(): LocationLike {
  return window.location;
}

function wsScheme(loc: LocationLike): string {
  return loc.protocol === "https:" ? "wss:" : "ws:";
}

export function originWsUrl(loc: LocationLike = currentLocation()): string {
  return `${wsScheme(loc)}//${loc.host}${DEFAULT_PATH}`;
}

export function normalizeWsUrl(
  input: string,
  loc: LocationLike = currentLocation(),
): string | null {
  const trimmed = input.trim();
  if (!trimmed) return null;

  const body = trimmed.startsWith("//") ? trimmed.slice(2) : trimmed;

  let candidate: string;
  if (/^http:\/\//i.test(body)) {
    candidate = `ws://${body.slice("http://".length)}`;
  } else if (/^https:\/\//i.test(body)) {
    candidate = `wss://${body.slice("https://".length)}`;
  } else if (SCHEME_RE.test(body)) {
    candidate = body;
  } else {
    candidate = `${wsScheme(loc)}//${body}`;
  }

  let url: URL;
  try {
    url = new URL(candidate);
  } catch {
    return null;
  }

  if (url.protocol !== "ws:" && url.protocol !== "wss:") return null;
  if (!url.hostname) return null;
  if (url.pathname === "" || url.pathname === "/") url.pathname = DEFAULT_PATH;

  return url.toString();
}

export function readStoredWsUrl(): string | null {
  try {
    return localStorage.getItem(WS_URL_STORAGE_KEY);
  } catch {
    return null;
  }
}

export function storeWsUrl(url: string): void {
  try {
    localStorage.setItem(WS_URL_STORAGE_KEY, url);
  } catch {}
}

export function clearStoredWsUrl(): void {
  try {
    localStorage.removeItem(WS_URL_STORAGE_KEY);
  } catch {}
}

function queryWsUrl(loc: LocationLike): string | null {
  try {
    return new URLSearchParams(loc.search).get(WS_URL_QUERY_KEY);
  } catch {
    return null;
  }
}

export function resolveWsUrl(loc: LocationLike = currentLocation()): ResolvedWsUrl {
  const candidates: [WsUrlSource, string | null | undefined][] = [
    ["query", queryWsUrl(loc)],
    ["stored", readStoredWsUrl()],
    ["env", import.meta.env.VITE_WS_URL],
  ];

  for (const [source, raw] of candidates) {
    if (!raw) continue;
    const normalized = normalizeWsUrl(raw, loc);
    if (normalized) return { url: normalized, source };
  }

  return { url: originWsUrl(loc), source: "origin" };
}
