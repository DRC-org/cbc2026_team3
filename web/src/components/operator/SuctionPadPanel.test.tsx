import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { SuctionPadPanel } from "@/components/operator/SuctionPadPanel";
import type { SuctionState } from "@/lib/protocol";
import { MALFORMED } from "@/lib/protocol";

const THREE_PADS: SuctionState = {
  pads: [
    { axis: "valve_1", label: "1", enabled: true },
    { axis: "valve_2", label: "2", enabled: false },
    { axis: "valve_3", label: "3", enabled: true },
  ],
};

function renderPanel(
  suction: SuctionState | typeof MALFORMED,
  blockedReason: string | null = null,
) {
  const send = vi.fn(() => true);
  const view = render(
    <SuctionPadPanel
      robotKey="sub_hand"
      suction={suction}
      blockedReason={blockedReason}
      sendOrReport={send}
    />,
  );
  return { send, view };
}

describe("SuctionPadPanel", () => {
  it("サーバーが配ったラベルで並べ、軸名は画面に出さない", () => {
    renderPanel(THREE_PADS);

    const group = screen.getByRole("group", { name: "吸着に使うパッド" });
    expect(group).toHaveTextContent("123");
    expect(screen.queryByText(/valve_/)).toBeNull();
  });

  it("ON のパッドは押された状態で示す", () => {
    renderPanel(THREE_PADS);

    expect(screen.getByRole("button", { name: "パッド 1 を使わない" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByRole("button", { name: "パッド 2 を使う" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
  });

  it("外すと、残る弁の全集合を自分の担当機へ宛てて送る", async () => {
    const { send } = renderPanel(THREE_PADS);

    await userEvent.click(screen.getByRole("button", { name: "パッド 1 を使わない" }));

    expect(send).toHaveBeenCalledWith(
      { type: "suction_pads_set", robot: "sub_hand", pads: ["valve_3"] },
      expect.any(String),
    );
  });

  it("使うにすると、その弁を配信順の位置へ足した全集合を送る", async () => {
    const { send } = renderPanel(THREE_PADS);

    await userEvent.click(screen.getByRole("button", { name: "パッド 2 を使う" }));

    expect(send).toHaveBeenCalledWith(
      { type: "suction_pads_set", robot: "sub_hand", pads: ["valve_1", "valve_2", "valve_3"] },
      expect.any(String),
    );
  });

  it("使用数を出す", () => {
    renderPanel(THREE_PADS);

    expect(screen.getByText("使用 2/3")).toBeInTheDocument();
  });

  it("1 つも選んでいなければ吸着ステップが拒否されると警告する", () => {
    renderPanel({ pads: THREE_PADS.pads.map((pad) => ({ ...pad, enabled: false })) });

    expect(screen.getByText(/吸着ステップは拒否されます/)).toBeInTheDocument();
  });

  it("塞がれているときは理由を出してボタンを無効にする", async () => {
    const { send } = renderPanel(THREE_PADS, "切断中のため変更できません");

    expect(screen.getByText("切断中のため変更できません")).toBeInTheDocument();
    const button = screen.getByRole("button", { name: "パッド 2 を使う" });
    expect(button).toBeDisabled();
    await userEvent.click(button);
    expect(send).not.toHaveBeenCalled();
  });

  it("次の吸着ステップから効くことを断る", () => {
    renderPanel(THREE_PADS);

    expect(screen.getByText(/次の「ワーク吸着」ステップから効きます/)).toBeInTheDocument();
  });

  it("読めない配信では操作を出さず、読めなかったと言う", () => {
    renderPanel(MALFORMED);

    expect(screen.getByText("吸着パッドの状態を読み取れませんでした")).toBeInTheDocument();
    expect(screen.queryByRole("button")).toBeNull();
  });
});
