import { useCallback, useEffect, useRef, useState } from "react";

export const HOLD_DELAY_MS = 400;
export const HOLD_INTERVAL_MS = 150;
export const HOLD_ACCEL_EVERY = 6;

export interface RepeatController {
  start: () => void;
  stop: () => void;
  multiplier: number;
}

export function useRepeatController(
  fire: (multiplier: number) => void,
  enabled: boolean,
  maxMultiplier = 1,
): RepeatController {
  const fireRef = useRef(fire);
  fireRef.current = fire;
  const maxRef = useRef(maxMultiplier);
  maxRef.current = maxMultiplier;

  const delayRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const countRef = useRef(0);

  const [multiplier, setMultiplier] = useState(1);

  const stop = useCallback(() => {
    if (delayRef.current !== null) {
      clearTimeout(delayRef.current);
      delayRef.current = null;
    }
    if (intervalRef.current !== null) {
      clearInterval(intervalRef.current);
      intervalRef.current = null;
    }
    countRef.current = 0;
    setMultiplier(1);
  }, []);

  const tick = useCallback(() => {
    const step = 2 ** Math.floor(countRef.current / HOLD_ACCEL_EVERY);
    const capped = Math.max(1, Math.min(step, maxRef.current));
    countRef.current += 1;
    setMultiplier(capped);
    fireRef.current(capped);
  }, []);

  const start = useCallback(() => {
    if (!enabled) return;
    stop();
    tick();
    delayRef.current = setTimeout(() => {
      delayRef.current = null;
      intervalRef.current = setInterval(tick, HOLD_INTERVAL_MS);
    }, HOLD_DELAY_MS);
  }, [enabled, stop, tick]);

  useEffect(() => stop, [stop]);

  useEffect(() => {
    if (!enabled) stop();
  }, [enabled, stop]);

  return { start, stop, multiplier };
}
