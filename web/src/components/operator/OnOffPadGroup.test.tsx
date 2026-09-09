import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { OnOffPadGroup, splitOnOffAxes } from "@/components/operator/OnOffPadGroup";
import type { ManualAxis } from "@/lib/protocol";

const VALVE: ManualAxis = {
  name: "valve_1",
  unit: "on_off",
  command_mode: "on_off",
  value: null,
  target: null,
  manual: null,
  manual_always: true,
  deviation: null,
  sync_tolerance: null,
  positions: [
    { name: "closed", value: 0 },
    { name: "open", value: 1 },
  ],
  motors: ["valve_1"],
};

const CONVEYOR: ManualAxis = {
  ...VALVE,
  name: "conveyor",
  unit: "duty",
  command_mode: "duty",
  positions: [
    { name: "stop", value: 0 },
    { name: "run", value: 0.3 },
  ],
  motors: ["conveyor"],
};

function valve(name: string, target: number | null): ManualAxis {
  return { ...VALVE, name, target, motors: [name] };
}

function renderGroup(axes: ManualAxis[], blockedReason: string | null = null) {
  const onMove = vi.fn();
  const view = render(<OnOffPadGroup axes={axes} blockedReason={blockedReason} onMove={onMove} />);
  return { onMove, view };
}

describe("OnOffPadGroup", () => {
  it("ON の軸は押された状態で示し、押すと OFF 側の位置名を送る", async () => {
    const { onMove } = renderGroup([valve("valve_1", 1)]);

    const button = screen.getByRole("button", { name: "valve_1 を OFF にする" });
    expect(button).toHaveAttribute("aria-pressed", "true");
    expect(button).toHaveTextContent("ON");
    expect(button).toHaveClass("bg-success");

    await userEvent.click(button);
    expect(onMove).toHaveBeenCalledWith("valve_1", "closed");
  });

  it("OFF の軸を押すと ON 側の位置名を送る", async () => {
    const { onMove } = renderGroup([valve("valve_2", 0)]);

    const button = screen.getByRole("button", { name: "valve_2 を ON にする" });
    expect(button).toHaveAttribute("aria-pressed", "false");
    expect(button).toHaveTextContent("OFF");
    expect(button).not.toHaveClass("bg-success");

    await userEvent.click(button);
    expect(onMove).toHaveBeenCalledWith("valve_2", "open");
  });

  it("一度も指令していない軸は OFF と別の見た目で描き、押すと ON 側へ送る", async () => {
    const { onMove } = renderGroup([valve("valve_3", null), valve("valve_4", 0)]);

    const unknown = screen.getByRole("button", { name: "valve_3 を ON にする" });
    expect(unknown).toHaveAttribute("aria-pressed", "false");
    expect(unknown).toHaveTextContent("—");
    expect(unknown).toHaveClass("border-dashed");
    expect(screen.getByRole("button", { name: "valve_4 を ON にする" })).not.toHaveClass(
      "border-dashed",
    );

    await userEvent.click(unknown);
    expect(onMove).toHaveBeenCalledWith("valve_3", "open");
  });

  it("軸名を円の下に添える", () => {
    renderGroup([valve("valve_5", 0)]);

    expect(screen.getByText("valve_5")).toBeInTheDocument();
  });

  it("塞がれているときは 1 つも押せない", async () => {
    const { onMove } = renderGroup([valve("valve_1", 1), valve("valve_2", 0)], "緊急停止中");

    for (const button of screen.getAllByRole("button")) {
      expect(button).toBeDisabled();
      await userEvent.click(button);
    }
    expect(onMove).not.toHaveBeenCalled();
  });

  it("扱える軸が無ければ何も描かない", () => {
    const { view } = renderGroup([]);

    expect(view.container).toBeEmptyDOMElement();
  });
});

describe("splitOnOffAxes", () => {
  it("ON / OFF の両方を配信された on_off 軸だけを群へ回す", () => {
    const { pads, rest } = splitOnOffAxes([valve("valve_1", 0), CONVEYOR]);

    expect(pads.map((axis) => axis.name)).toEqual(["valve_1"]);
    expect(rest.map((axis) => axis.name)).toEqual(["conveyor"]);
  });

  it("ON 側 / OFF 側のどちらかが欠けた on_off 軸は推測せず行へ落とす", () => {
    const noOn: ManualAxis = { ...valve("valve_2", 0), positions: [{ name: "closed", value: 0 }] };
    const noOff: ManualAxis = { ...valve("valve_3", 0), positions: [{ name: "open", value: 1 }] };
    const noValue: ManualAxis = {
      ...valve("valve_4", 0),
      positions: [
        { name: "closed", value: null },
        { name: "open", value: null },
      ],
    };

    const { pads, rest } = splitOnOffAxes([noOn, noOff, noValue]);

    expect(pads).toEqual([]);
    expect(rest.map((axis) => axis.name)).toEqual(["valve_2", "valve_3", "valve_4"]);
  });
});
