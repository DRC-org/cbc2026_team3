export interface TabDef {
  path: string;
  label: string;
  /** 切替に割り当てる数字キー。タブバー上にも表示して発見できるようにする */
  hotkey: string;
  /** バッジ表示のために監視するロボット。Monitor / PID Tuning は対象外 */
  robotKey?: string;
}

export const TABS: TabDef[] = [
  { path: "/monitor", label: "Monitor", hotkey: "1" },
  { path: "/main-hand", label: "Main Hand", hotkey: "2", robotKey: "main_hand" },
  { path: "/sub-hand", label: "Sub Hand", hotkey: "3", robotKey: "sub_hand" },
  { path: "/pid-tuning", label: "PID Tuning", hotkey: "4" },
];

export const DEFAULT_TAB_PATH = TABS[0].path;

/** ハッシュ運用時代のタブ ID。旧ブックマークからの流入を受けるためだけに残す */
const LEGACY_HASH_IDS = new Set(TABS.map((tab) => tab.path.slice(1)));

/**
 * `#main-hand` 形式の旧ブックマークをパスへ読み替える。
 *
 * 各操縦者が自分の担当タブの URL をブックマークして試合に臨む運用のため、
 * ハッシュからパスへ移行した後も旧 URL が Monitor に落ちてはならない。
 * 対象外なら null を返す（ルーターの通常処理に委ねる）。
 */
export function legacyHashTarget(location: {
  pathname: string;
  search: string;
  hash: string;
}): string | null {
  if (location.pathname !== "/") return null;
  const id = location.hash.replace(/^#/, "");
  if (!LEGACY_HASH_IDS.has(id)) return null;
  return `/${id}${location.search}`;
}
