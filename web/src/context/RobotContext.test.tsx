import { render } from "@testing-library/react";
import { memo } from "react";
import { describe, expect, it } from "vitest";

import {
  RobotProvider,
  useRobotCommands,
  useRobotStates,
  useRobotStatus,
} from "@/context/RobotContext";
import type { RobotContextValue } from "@/context/RobotContext";
import type { MatchState, RobotState } from "@/lib/protocol";
import { createRobotContext } from "@/test/robotContext";

const renders = { states: 0, status: 0, commands: 0 };

const StatesProbe = memo(function StatesProbe() {
  useRobotStates();
  renders.states += 1;
  return null;
});

const StatusProbe = memo(function StatusProbe() {
  useRobotStatus();
  renders.status += 1;
  return null;
});

const CommandsProbe = memo(function CommandsProbe() {
  useRobotCommands();
  renders.commands += 1;
  return null;
});

const Shell = memo(function Shell() {
  return (
    <>
      <StatesProbe />
      <StatusProbe />
      <CommandsProbe />
    </>
  );
});

function telemetry(stepIndex: number): Record<string, RobotState> {
  return { main_hand: { step_index: stepIndex } as RobotState };
}

function setup() {
  renders.states = 0;
  renders.status = 0;
  renders.commands = 0;
  const base = createRobotContext();
  const view = render(
    <RobotProvider value={base}>
      <Shell />
    </RobotProvider>,
  );
  const update = (over: Partial<RobotContextValue>) =>
    view.rerender(
      <RobotProvider value={{ ...base, ...over }}>
        <Shell />
      </RobotProvider>,
    );
  return { base, update };
}

describe("RobotProvider の購読分割", () => {
  it("テレメトリ更新で再描画されるのはテレメトリ購読者だけ", () => {
    const { update } = setup();
    expect(renders).toEqual({ states: 1, status: 1, commands: 1 });

    update({ states: telemetry(1) });

    expect(renders.states).toBe(2);
    expect(renders.status).toBe(1);
    expect(renders.commands).toBe(1);
  });

  it("20Hz × 2 台ぶん (毎秒 40 通) の配信でも低頻度側は再描画されない", () => {
    const { update } = setup();

    for (let i = 0; i < 40; i++) update({ states: telemetry(i) });

    expect(renders.states).toBe(41);
    expect(renders.status).toBe(1);
    expect(renders.commands).toBe(1);
  });

  it("試合状態の変化はテレメトリ購読者を巻き込まない", () => {
    const { base, update } = setup();
    const matchState: MatchState = { ...base.matchState, phase: "match" };

    update({ matchState });

    expect(renders.status).toBe(2);
    expect(renders.states).toBe(1);
    expect(renders.commands).toBe(1);
  });

  it("同じ値で再描画しても購読者は動かない", () => {
    const { update } = setup();

    update({});

    expect(renders).toEqual({ states: 1, status: 1, commands: 1 });
  });
});
