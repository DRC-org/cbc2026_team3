/**
 * WebSocket 接続先の解決。
 *
 * 既定はページ origin だが、それだけでは成立しない構成がある:
 * - vite dev (5173) を Tailscale 経由で開き、制御プログラム (8080) へ直接繋ぎたい
 * - Cloudflare へ配信した UI から、手元の制御 PC へ繋ぎたい
 * - 制御 PC を予備機に切り替えた
 * いずれも「配信元 ≠ 制御プログラム」なので、接続先を後から差し替えられる必要がある。
 */

export type WsUrlSource = "query" | "stored" | "env" | "origin";

export interface ResolvedWsUrl {
  url: string;
  source: WsUrlSource;
}

/** window.location のうち接続先解決に使う部分だけ（テストから差し替えるため） */
export interface LocationLike {
  protocol: string;
  host: string;
  search: string;
}

export const WS_URL_STORAGE_KEY = "cbc2026.ws_url";
/** URL に載せる一時上書き用のクエリキー。操縦者ごとのブックマークに使える */
export const WS_URL_QUERY_KEY = "ws";

const DEFAULT_PATH = "/ws";
const SCHEME_RE = /^[a-z][a-z0-9+.-]*:\/\//i;

function currentLocation(): LocationLike {
  return window.location;
}

function wsScheme(loc: LocationLike): string {
  return loc.protocol === "https:" ? "wss:" : "ws:";
}

/** 配信元と同じホスト・ポートの /ws。別端末から開いても自分の localhost を見に行かない */
export function originWsUrl(loc: LocationLike = currentLocation()): string {
  return `${wsScheme(loc)}//${loc.host}${DEFAULT_PATH}`;
}

/**
 * 操縦者の手入力を WebSocket URL に正規化する。不正なら null。
 *
 * 試合会場で急いで打ち込む場面を想定し、`drc:8080` のような省略形と、
 * ブラウザからコピーした `http://...` を受け付ける。
 */
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
  } catch {
    // 保存できなくても現セッションの接続先は切り替わる。UI を落とす方が損害が大きい
  }
}

export function clearStoredWsUrl(): void {
  try {
    localStorage.removeItem(WS_URL_STORAGE_KEY);
  } catch {
    // 同上
  }
}

function queryWsUrl(loc: LocationLike): string | null {
  try {
    return new URLSearchParams(loc.search).get(WS_URL_QUERY_KEY);
  } catch {
    return null;
  }
}

/**
 * 接続先を決める。クエリ > 保存済み > ビルド時 env > ページ origin。
 *
 * クエリを最優先かつ非永続にしているのは、保存済み設定を壊さずに
 * 「この画面だけ別の制御 PC を見る」を成立させるため。
 */
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
