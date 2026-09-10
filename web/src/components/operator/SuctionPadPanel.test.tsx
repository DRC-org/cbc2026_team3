import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { SuctionPadPanel } from "@/components/operator/SuctionPadPanel";
import type { ManualAxis, ManualPosition, ManualState, SuctionState } from "@/lib/protocol";
import { MALFORMED } from "@/lib/protocol";

const THREE_PADS: SuctionState = {
  pads: [
    { axis: "valve_1", label: "1", enabled: true },
    { axis: "valve_2", label: "2", enabled: false },
    { axis: "valve_3", label: "3", enabled: true },
  ],
  fill_from: null,
};

const OPEN_CLOSED: ManualPosition[] = [
  { name: "closed", value: 0 },
  { name: "open", value: 1 },
];

function valveAxis(
  name: string,
  target: number | null,
  positions: ManualPosition[] = OPEN_CLOSED,
): ManualAxis {
  return {
    name,
    unit: "on_off",
    command_mode: "on_off",
    value: null,
    target,
    manual: null,
    manual_always: true,
    deviation: null,
    sync_tolerance: null,
    positions,
    motors: [name],
  };
}

const SEQUENCE: ManualState = { mode: "sequence", axes: [] };

function manualWith(axes: ManualAxis[]): ManualState {
  return { mode: "manual", axes };
}

function renderPanel(
  suction: SuctionState | typeof MALFORMED,
  blockedReason: string | null = null,
  manual: ManualState = SEQUENCE,
) {
  const send = vi.fn(() => true);
  const view = render(
    <SuctionPadPanel
      robotKey="sub_hand"
      suction={suction}
      manual={manual}
      blockedReason={blockedReason}
      sendOrReport={send}
    />,
  );
  return { send, view };
}

describe("SuctionPadPanel", () => {
  it.each([
    ["left", "左端から順に ON にしてください"],
    ["right", "右端から順に ON にしてください"],
  ] as const)("半自動では ON にしていく端 (%s) を配信のまま出す", (fillFrom, text) => {
    renderPanel({ ...THREE_PADS, fill_from: fillFrom });

    expect(screen.getByText(new RegExp(text))).toBeInTheDocument();
  });

  it("コート未確定では端を断定しない", () => {
    renderPanel({ ...THREE_PADS, fill_from: null });

    expect(screen.queryByText(/端から順に/)).toBeNull();
    expect(screen.getByText(/コートが未確定/)).toBeInTheDocument();
  });

  it("手動では端の話を出さない (宣言ではなく今すぐ開閉の面)", () => {
    renderPanel({ ...THREE_PADS, fill_from: "left" }, null, manualWith([valveAxis("valve_1", 1)]));

    expect(screen.queryByText(/端から順に/)).toBeNull();
  });

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
    renderPanel({
      ...THREE_PADS,
      pads: THREE_PADS.pads.map((pad) => ({ ...pad, enabled: false })),
    });

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

  describe("手動モード", () => {
    const AXES = [valveAxis("valve_1", 1), valveAxis("valve_2", 0), valveAxis("valve_3", null)];

    function renderManual(blockedReason: string | null = null, axes: ManualAxis[] = AXES) {
      return renderPanel(THREE_PADS, blockedReason, manualWith(axes));
    }

    it("円の中は配信のラベルのまま、弁の名前は画面に出さない", () => {
      renderManual();

      const group = screen.getByRole("group", { name: "吸着パッドの開閉" });
      expect(group).toHaveTextContent("123");
      expect(screen.queryByText(/valve_/)).toBeNull();
    });

    it("開いている弁を押すと OFF 側の位置名で manual_move を送る", async () => {
      const { send } = renderManual();

      const button = screen.getByRole("button", { name: "パッド 1 を閉じる" });
      expect(button).toHaveAttribute("aria-pressed", "true");
      expect(button).toHaveClass("bg-success");

      await userEvent.click(button);

      expect(send).toHaveBeenCalledWith(
        { type: "manual_move", robot: "sub_hand", axis: "valve_1", position: "closed" },
        expect.any(String),
      );
    });

    it("閉じている弁を押すと ON 側の位置名で manual_move を送る", async () => {
      const { send } = renderManual();

      const button = screen.getByRole("button", { name: "パッド 2 を開く" });
      expect(button).toHaveAttribute("aria-pressed", "false");
      expect(button).not.toHaveClass("border-dashed");

      await userEvent.click(button);

      expect(send).toHaveBeenCalledWith(
        { type: "manual_move", robot: "sub_hand", axis: "valve_2", position: "open" },
        expect.any(String),
      );
    });

    it("一度も指令していない弁は OFF と別の見た目で描き、押すと ON 側へ送る", async () => {
      const { send } = renderManual();

      const button = screen.getByRole("button", { name: "パッド 3 を開く" });
      expect(button).toHaveClass("border-dashed");

      await userEvent.click(button);

      expect(send).toHaveBeenCalledWith(
        { type: "manual_move", robot: "sub_hand", axis: "valve_3", position: "open" },
        expect.any(String),
      );
    });

    it("対応する軸が配信に無いパッドは、そのパッドだけ押せない", async () => {
      const { send } = renderManual(null, [valveAxis("valve_1", 1), valveAxis("valve_3", 0)]);

      const missing = screen.getByRole("button", { name: "パッド 2 を開く" });
      expect(missing).toBeDisabled();
      expect(missing).toHaveClass("border-dashed");
      await userEvent.click(missing);
      expect(send).not.toHaveBeenCalled();

      expect(screen.getByRole("button", { name: "パッド 1 を閉じる" })).toBeEnabled();
    });

    it("ON 側 / OFF 側のどちらかが欠けたパッドは推測せず押せない", async () => {
      const { send } = renderManual(null, [
        valveAxis("valve_1", 1, [{ name: "open", value: 1 }]),
        valveAxis("valve_2", 0, [{ name: "closed", value: 0 }]),
        valveAxis("valve_3", 0),
      ]);

      for (const name of ["パッド 1 を閉じる", "パッド 2 を開く"]) {
        const button = screen.getByRole("button", { name });
        expect(button).toBeDisabled();
        await userEvent.click(button);
      }
      expect(send).not.toHaveBeenCalled();

      expect(screen.getByRole("button", { name: "パッド 3 を開く" })).toBeEnabled();
    });

    it("開いている数を出し、0 個でも吸着ステップの警告は出さない", () => {
      renderManual(null, [
        valveAxis("valve_1", 0),
        valveAxis("valve_2", 0),
        valveAxis("valve_3", 0),
      ]);

      expect(screen.getByText("開 0/3")).toBeInTheDocument();
      expect(screen.queryByText(/吸着ステップは拒否されます/)).toBeNull();
    });

    it("押した弁が今すぐ開くことを断る", () => {
      renderManual();

      expect(screen.getByText(/押した弁が今すぐ開きます/)).toBeInTheDocument();
      expect(screen.queryByText(/次の「ワーク吸着」ステップから効きます/)).toBeNull();
    });

    it("塞がれているときは 1 つも押せない", async () => {
      const { send } = renderManual("緊急停止中は手動操縦できません");

      expect(screen.getByText("緊急停止中は手動操縦できません")).toBeInTheDocument();
      for (const button of screen.getAllByRole("button")) {
        expect(button).toBeDisabled();
        await userEvent.click(button);
      }
      expect(send).not.toHaveBeenCalled();
    });
  });
});
