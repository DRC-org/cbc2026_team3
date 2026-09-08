import { useCallback, useEffect, useRef, useState } from "react";

export const ARM_GUARD_MS = 400;

export const ARM_TIMEOUT_MS = 4000;

type ArmState = "idle" | "guard" | "armed";

export interface ArmedPress {
  armed: boolean;
  press: () => void;
  disarm: () => void;
}

export function useArmedPress(fire: () => void): ArmedPress {
  const fireRef = useRef(fire);
  fireRef.current = fire;

  // 判定は ref が正。state 更新関数の中で判定すると StrictMode の 2 度呼びで 2 回発火する。
  const stateRef = useRef<ArmState>("idle");
  const [armed, setArmed] = useState(false);

  const guardRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const timeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const clearTimers = useCallback(() => {
    if (guardRef.current !== null) {
      clearTimeout(guardRef.current);
      guardRef.current = null;
    }
    if (timeoutRef.current !== null) {
      clearTimeout(timeoutRef.current);
      timeoutRef.current = null;
    }
  }, []);

  const transition = useCallback((next: ArmState) => {
    stateRef.current = next;
    setArmed(next !== "idle");
  }, []);

  const disarm = useCallback(() => {
    clearTimers();
    transition("idle");
  }, [clearTimers, transition]);

  const press = useCallback(() => {
    const prev = stateRef.current;

    if (prev === "guard") return;

    if (prev === "armed") {
      clearTimers();
      transition("idle");
      fireRef.current();
      return;
    }

    clearTimers();
    transition("guard");
    guardRef.current = setTimeout(() => {
      guardRef.current = null;
      if (stateRef.current === "guard") transition("armed");
    }, ARM_GUARD_MS);
    timeoutRef.current = setTimeout(() => {
      timeoutRef.current = null;
      clearTimers();
      transition("idle");
    }, ARM_TIMEOUT_MS);
  }, [clearTimers, transition]);

  useEffect(() => clearTimers, [clearTimers]);

  return { armed, press, disarm };
}
