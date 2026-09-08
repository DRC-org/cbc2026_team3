import { useEffect, useReducer, useRef } from "react";

import { Panel } from "@/components/ui/Panel";
import type { MatchTimer as MatchTimerValue } from "@/lib/protocol";

const BOUNDARY_EPSILON_MS = 15;

interface Anchor {
  elapsedMs: number;
  atPerfMs: number;
}

function clampRemaining(remainingMs: number, durationMs: number): number {
  return Math.min(Math.max(remainingMs, 0), durationMs);
}

export function formatRemaining(remainingMs: number): string {
  const totalSeconds = Math.ceil(remainingMs / 1000);
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${minutes}:${String(seconds).padStart(2, "0")}`;
}

function msUntilDisplayChange(remainingMs: number): number {
  return remainingMs - (Math.ceil(remainingMs / 1000) - 1) * 1000;
}

interface MatchTimerProps {
  timer: MatchTimerValue | null;
}

export function MatchTimer({ timer }: MatchTimerProps) {
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
      const elapsedNow = anchor.current.elapsedMs + (performance.now() - anchor.current.atPerfMs);
      const remaining = clampRemaining(durationMs - elapsedNow, durationMs);
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

  if (!timer) {
    return (
      <Panel legend="試合時間" className="shrink-0">
        <div className="text-center text-[1.1em] text-base-content/60">タイマー未受信</div>
      </Panel>
    );
  }

  const elapsedNow = running
    ? anchor.current.elapsedMs + (performance.now() - anchor.current.atPerfMs)
    : elapsedMs;
  const remaining = clampRemaining(durationMs - elapsedNow, durationMs);

  const caption = running ? null : elapsedMs === 0 ? "開始前" : "試合終了時点の残り";

  return (
    <Panel legend="試合時間" className="shrink-0">
      <div className="flex flex-col items-center gap-[0.1em] py-1">
        <span className="font-mono text-[3.4em] leading-none font-bold tabular-nums">
          {formatRemaining(remaining)}
        </span>
        {caption ? <span className="text-[0.8em] text-base-content/60">{caption}</span> : null}
      </div>
    </Panel>
  );
}
