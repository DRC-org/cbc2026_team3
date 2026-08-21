import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { Checklist } from "@/components/Checklist";
import type { ChecklistState, MatchPhase } from "@/hooks/useRobotSocket";
import { DEFAULT_MATCH_STATE, renderWithRobot } from "@/test/robotContext";

const ITEMS: ChecklistState = {
  items: [
    { id: "power", label: "電源投入", checked: true },
    { id: "estop", label: "非常停止解除", checked: false },
  ],
  completed: false,
};

function mount(checklist: ChecklistState | undefined, phase: MatchPhase = "setup") {
  return renderWithRobot(<Checklist checklistRole="main_hand" title="メインハンド" />, {
    matchState: {
      ...DEFAULT_MATCH_STATE,
      phase,
      checklists: checklist ? { main_hand: checklist } : {},
    },
  });
}

describe("Checklist", () => {
  it("項目とチェック済み件数を表示する", () => {
    mount(ITEMS);

    expect(screen.getByLabelText("電源投入")).toBeChecked();
    expect(screen.getByLabelText("非常停止解除")).not.toBeChecked();
    expect(screen.getByText("1 / 2 項目")).toBeInTheDocument();
  });

  it("項目が未定義なら設定ファイルの場所を案内する", () => {
    mount(undefined);
    expect(screen.getByText(/config\/checklist\.yaml/)).toBeInTheDocument();
  });

  it("チェック操作をサーバーへ送る (状態はサーバー保持のため)", async () => {
    const { context } = mount(ITEMS);

    await userEvent.click(screen.getByLabelText("非常停止解除"));
    expect(context.setChecklistItem).toHaveBeenCalledWith("main_hand", "estop", true);
  });

  it("チェック済み項目を外す操作も送る", async () => {
    const { context } = mount(ITEMS);

    await userEvent.click(screen.getByLabelText("電源投入"));
    expect(context.setChecklistItem).toHaveBeenCalledWith("main_hand", "power", false);
  });

  it("CLEAR で一括解除を送る", async () => {
    const { context } = mount(ITEMS);

    await userEvent.click(screen.getByRole("button", { name: /チェックをすべて解除/ }));
    expect(context.resetChecklist).toHaveBeenCalledWith("main_hand");
  });

  it("チェックが 0 件なら CLEAR を押せない", () => {
    mount({ items: [{ id: "power", label: "電源投入", checked: false }], completed: false });
    expect(screen.getByRole("button", { name: /チェックをすべて解除/ })).toBeDisabled();
  });

  it("完了時は完了表示を出す", () => {
    mount({ ...ITEMS, completed: true });
    expect(screen.getByText("✓ 指差喚呼 完了")).toBeInTheDocument();
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
});
