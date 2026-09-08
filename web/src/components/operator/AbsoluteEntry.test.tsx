import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { AbsoluteEntry } from "@/components/operator/AbsoluteEntry";
import type { ManualAxis } from "@/lib/protocol";

function axisOf(over: Partial<ManualAxis> = {}): ManualAxis {
  return {
    name: "y_axis",
    unit: "mm",
    command_mode: "position",
    value: 20,
    target: 20,
    manual: { min: 0, max: 20, steps: [1] },
    ...over,
  } as ManualAxis;
}

const LABEL = { name: "y_axis の目標値" };
const SEND = { name: "y_axis を入力値へ移動" };

function mount(axis: ManualAxis, onSet = vi.fn()) {
  render(<AbsoluteEntry axis={axis} min={0} max={20} disabled={false} onSet={onSet} />);
  return onSet;
}

describe("AbsoluteEntry", () => {
  it("入力した値をそのまま送る (クランプは UI 側で行わない)", async () => {
    const user = userEvent.setup();
    const onSet = mount(axisOf({ target: 5, value: 5 }));

    const input = screen.getByRole("spinbutton", LABEL);
    await user.clear(input);
    await user.type(input, "12");
    await user.click(screen.getByRole("button", SEND));

    expect(onSet).toHaveBeenCalledWith("y_axis", 12);
  });

  it("範囲外を送った後、入力欄に今の目標値が戻る", async () => {
    const user = userEvent.setup();
    const onSet = mount(axisOf({ target: 20, value: 20 }));

    const input = screen.getByRole("spinbutton", LABEL);
    await user.clear(input);
    await user.type(input, "999");
    expect(input).toHaveValue(999);

    await user.click(screen.getByRole("button", SEND));

    expect(onSet).toHaveBeenCalledWith("y_axis", 999);
    expect(input).toHaveValue(20);
    expect(input.className).not.toContain("border-warning");
  });

  it("編集中はサーバーの配信で入力欄を書き換えない", async () => {
    const user = userEvent.setup();
    const onSet = vi.fn();
    const view = render(
      <AbsoluteEntry
        axis={axisOf({ target: 5 })}
        min={0}
        max={20}
        disabled={false}
        onSet={onSet}
      />,
    );

    const input = screen.getByRole("spinbutton", LABEL);
    await user.clear(input);
    await user.type(input, "12");

    view.rerender(
      <AbsoluteEntry
        axis={axisOf({ target: 7, value: 7 })}
        min={0}
        max={20}
        disabled={false}
        onSet={onSet}
      />,
    );

    expect(input).toHaveValue(12);
  });
});
