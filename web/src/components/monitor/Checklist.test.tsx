import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { Checklist } from "@/components/monitor/Checklist";
import type { ChecklistState, MatchPhase } from "@/lib/protocol";
import { DEFAULT_MATCH_STATE, DEFAULT_SERVER_INFO, renderWithRobot } from "@/test/robotContext";

const ITEMS: ChecklistState = {
  items: [
    { id: "power", label: "電源投入", checked: true },
    { id: "estop", label: "非常停止解除", checked: false },
  ],
  completed: false,
};

function mount(
  checklist: ChecklistState | undefined,
  phase: MatchPhase = "setup",
  devTools = false,
) {
  return renderWithRobot(<Checklist />, {
    matchState: {
      ...DEFAULT_MATCH_STATE,
      phase,
      checklists: checklist ? { pre_match: checklist } : {},
    },
    serverInfo: { ...DEFAULT_SERVER_INFO, dev_tools: devTools },
  });
}

const DEV_BUTTON = /開発用に全てチェック/;

describe("Checklist", () => {
  it("項目とチェック済み件数を表示する", () => {
    mount(ITEMS);

    expect(screen.getByLabelText("電源投入")).toBeChecked();
    expect(screen.getByLabelText("非常停止解除")).not.toBeChecked();
    // 件数は「済 / 全」と残数の 2 通りで出す（毎回数え直させないため）
    expect(screen.getByText("/2")).toBeInTheDocument();
    expect(screen.getByText("残り 1")).toBeInTheDocument();
  });

  it("項目が未定義なら設定ファイルの場所を案内する", () => {
    mount(undefined);
    expect(screen.getByText(/config\/checklist\.yaml/)).toBeInTheDocument();
  });

  it("チェック操作をサーバーへ送る (状態はサーバー保持のため)", async () => {
    const { context } = mount(ITEMS);

    await userEvent.click(screen.getByLabelText("非常停止解除"));
    expect(context.setChecklistItem).toHaveBeenCalledWith("pre_match", "estop", true);
  });

  it("チェック済み項目を外す操作も送る", async () => {
    const { context } = mount(ITEMS);

    await userEvent.click(screen.getByLabelText("電源投入"));
    expect(context.setChecklistItem).toHaveBeenCalledWith("pre_match", "power", false);
  });

  it("CLEAR で一括解除を送る", async () => {
    const { context } = mount(ITEMS);

    await userEvent.click(screen.getByRole("button", { name: /チェックをすべて解除/ }));
    expect(context.resetChecklist).toHaveBeenCalledWith("pre_match");
  });

  it("チェックが 0 件なら CLEAR を押せない", () => {
    mount({ items: [{ id: "power", label: "電源投入", checked: false }], completed: false });
    expect(screen.getByRole("button", { name: /チェックをすべて解除/ })).toBeDisabled();
  });

  it("完了時は完了表示を出す", () => {
    mount({ ...ITEMS, completed: true });
    expect(screen.getByText("完了")).toBeInTheDocument();
  });

  it("未完の先頭だけを『次に唱える項目』として強調する", () => {
    mount({
      items: [
        { id: "power", label: "電源投入", checked: true },
        { id: "estop", label: "非常停止解除", checked: false },
        { id: "can", label: "CAN 確認", checked: false },
      ],
      completed: false,
    });

    // 未完は 2 件あるが、強調するのは先頭の 1 件だけ
    expect(screen.getAllByText("次")).toHaveLength(1);
    expect(screen.getByLabelText("非常停止解除").closest("label")).toHaveClass("border-l-warning");
    expect(screen.getByLabelText("CAN 確認").closest("label")).not.toHaveClass("border-l-warning");
  });

  it.each<MatchPhase>(["match", "finished"])("%s フェーズでは操作を締め切る", (phase) => {
    mount(ITEMS, phase);

    expect(screen.getByLabelText("電源投入")).toBeDisabled();
    expect(screen.getByLabelText("非常停止解除")).toBeDisabled();
    expect(screen.getByRole("button", { name: /チェックをすべて解除/ })).toBeDisabled();
  });

  it.each<MatchPhase>(["setup", "ready"])("%s フェーズでは操作できる", (phase) => {
    mount(ITEMS, phase);
    expect(screen.getByLabelText("電源投入")).toBeEnabled();
  });

  describe("開発用の一括チェック", () => {
    it("サーバーが開発用フラグを配っていなければボタンごと出さない", () => {
      mount(ITEMS);
      expect(screen.queryByRole("button", { name: DEV_BUTTON })).not.toBeInTheDocument();
    });

    it("開発用フラグが立っていれば押せる", async () => {
      const { context } = mount(ITEMS, "setup", true);

      await userEvent.click(screen.getByRole("button", { name: DEV_BUTTON }));
      expect(context.checkAllChecklist).toHaveBeenCalledWith("pre_match");
    });

    it("完了済みなら押せない (押しても変わらない操作を残さない)", () => {
      mount({ ...ITEMS, completed: true }, "setup", true);
      expect(screen.getByRole("button", { name: DEV_BUTTON })).toBeDisabled();
    });

    it.each<MatchPhase>(["match", "finished"])("%s フェーズでは押せない", (phase) => {
      mount(ITEMS, phase, true);
      expect(screen.getByRole("button", { name: DEV_BUTTON })).toBeDisabled();
    });
  });
});
