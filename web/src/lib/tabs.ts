export interface TabDef {
  path: string;
  label: string;
  hotkey: string;
  robotKey?: string;
}

export const TABS: TabDef[] = [
  { path: "/monitor", label: "Monitor", hotkey: "1" },
  { path: "/main-hand", label: "Main Hand", hotkey: "2", robotKey: "main_hand" },
  { path: "/sub-hand", label: "Sub Hand", hotkey: "3", robotKey: "sub_hand" },
];

export const DEFAULT_TAB_PATH = TABS[0].path;

const LEGACY_HASH_IDS = new Set(TABS.map((tab) => tab.path.slice(1)));

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

export function applyLegacyHashRedirect(): void {
  const target = legacyHashTarget(window.location);
  if (target) window.history.replaceState(null, "", target);
}
