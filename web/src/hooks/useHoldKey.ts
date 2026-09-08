import { useEffect, useRef } from "react";

import { useModalRegistry } from "@/context/ModalContext";
import { isHotkeyBlocked } from "@/hooks/useHotkeys";
import { useRepeatController } from "@/hooks/useRepeatController";

export function useHoldKey(
  key: string,
  fire: (multiplier: number) => void,
  enabled: boolean,
  maxMultiplier = 1,
): { multiplier: number } {
  const { openCount } = useModalRegistry();
  const modalOpenRef = useRef(openCount > 0);
  modalOpenRef.current = openCount > 0;

  const { start, stop, multiplier } = useRepeatController(fire, enabled, maxMultiplier);

  useEffect(() => {
    if (!enabled) return;

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== key) return;
      if (modalOpenRef.current || isHotkeyBlocked(event)) return;
      event.preventDefault();
      start();
    };
    const onKeyUp = (event: KeyboardEvent) => {
      if (event.key === key) stop();
    };
    const onHidden = () => {
      if (document.visibilityState === "hidden") stop();
    };

    window.addEventListener("keydown", onKeyDown);
    window.addEventListener("keyup", onKeyUp);
    window.addEventListener("blur", stop);
    document.addEventListener("visibilitychange", onHidden);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("keyup", onKeyUp);
      window.removeEventListener("blur", stop);
      document.removeEventListener("visibilitychange", onHidden);
      stop();
    };
  }, [key, enabled, start, stop]);

  return { multiplier };
}
