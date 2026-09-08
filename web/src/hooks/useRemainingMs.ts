import { useEffect, useReducer, useRef } from "react";

import type { MatchTimer as MatchTimerValue } from "@/lib/protocol";

const BOUNDARY_EPSILON_MS = 15;

interface Anchor {
  elapsedMs: number;
  atPerfMs: number;
}

function elapsedNowFrom(anchor: Anchor): number {
  return anchor.elapsedMs + (performance.now() - anchor.atPerfMs);
}

function clampRemaining(remainingMs: number, durationMs: number): number {
  return Math.min(Math.max(remainingMs, 0), durationMs);
}

function msUntilDisplayChange(remainingMs: number): number {
  return remainingMs - (Math.ceil(remainingMs / 1000) - 1) * 1000;
}

export function useRemainingMs(timer: MatchTimerValue | null | undefined): number | null {
  const [, tick] = useReducer((n: number) => n + 1, 0);

  const elapsedMs = timer?.elapsed_ms ?? 0;
  const durationMs = timer?.duration_ms ?? 0;
  const running = timer?.running ?? false;

  const anchor = useRef<Anchor>({ elapsedMs, atPerfMs: performance.now() });

  useEffect(() => {
    anchor.current = { elapsedMs, atPerfMs: performance.now() };
    tick();
  }, [elapsedMs, running]);

  useEffect(() => {
    if (!running || durationMs <= 0) return;

    let timeoutId = 0;
    const schedule = () => {
      const remaining = clampRemaining(durationMs - elapsedNowFrom(anchor.current), durationMs);
      if (remaining <= 0) return;

      timeoutId = window.setTimeout(
        () => {
          tick();
          schedule();
        },
        msUntilDisplayChange(remaining) + BOUNDARY_EPSILON_MS,
      );
    };
    schedule();

    return () => window.clearTimeout(timeoutId);
  }, [running, durationMs, elapsedMs]);

  if (!timer) return null;

  const elapsedNow = running ? elapsedNowFrom(anchor.current) : elapsedMs;
  return clampRemaining(durationMs - elapsedNow, durationMs);
}
