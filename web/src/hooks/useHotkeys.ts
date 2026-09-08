import { useEffect, useRef } from "react";

import { useModalRegistry } from "@/context/ModalContext";

export type HotkeyMap = Record<string, (() => void) | undefined>;

export function isHotkeyBlocked(event: KeyboardEvent): boolean {
  if (event.ctrlKey || event.metaKey || event.altKey || event.repeat) return true;

  const target = event.target as HTMLElement | null;
  if (target?.isContentEditable) return true;
  const tag = target?.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
}

function hotkeyNameOf(event: KeyboardEvent): string {
  return event.shiftKey ? `Shift+${event.key}` : event.key;
}

export function useHotkeys(map: HotkeyMap, enabled = true): void {
  const { openCount } = useModalRegistry();

  const mapRef = useRef(map);
  mapRef.current = map;
  const modalOpenRef = useRef(openCount > 0);
  modalOpenRef.current = openCount > 0;

  useEffect(() => {
    if (!enabled) return;

    const onKeyDown = (event: KeyboardEvent) => {
      if (modalOpenRef.current || isHotkeyBlocked(event)) return;
      const handler = mapRef.current[hotkeyNameOf(event)];
      if (!handler) return;
      event.preventDefault();
      handler();
    };

    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [enabled]);
}
