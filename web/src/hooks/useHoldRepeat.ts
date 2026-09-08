import { useRepeatController } from "@/hooks/useRepeatController";

export { HOLD_ACCEL_EVERY, HOLD_DELAY_MS, HOLD_INTERVAL_MS } from "@/hooks/useRepeatController";

export interface HoldHandlers {
  onPointerDown: () => void;
  onPointerUp: () => void;
  onPointerLeave: () => void;
  onPointerCancel: () => void;
  onBlur: () => void;
}

export interface HoldRepeat {
  handlers: HoldHandlers;
  multiplier: number;
}

export function useHoldRepeat(
  fire: (multiplier: number) => void,
  enabled = true,
  maxMultiplier = 1,
): HoldRepeat {
  const { start, stop, multiplier } = useRepeatController(fire, enabled, maxMultiplier);

  return {
    handlers: {
      onPointerDown: start,
      onPointerUp: stop,
      onPointerLeave: stop,
      onPointerCancel: stop,
      onBlur: stop,
    },
    multiplier,
  };
}
