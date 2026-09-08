import type { MotorState } from "@/lib/protocol";

export function motorState(overrides: Partial<MotorState> = {}): MotorState {
  return {
    pos: 0,
    vel: 0,
    torque: 0,
    temp: 30,
    command: null,
    command_mode: null,
    ...overrides,
  };
}
