import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { WsSettings } from "@/components/WsSettings";
import type { RobotContextValue } from "@/test/robotContext";
import { renderWithRobot } from "@/test/robotContext";

function mount(overrides: Partial<RobotContextValue> = {}, open = true) {
  const onClose = vi.fn();
  const view = renderWithRobot(<WsSettings open={open} onClose={onClose} />, overrides);
  return { ...view, onClose };
}

describe("WsSettings", () => {
  // TuiCss の Modal は閉じている間も DOM に残り .active の有無で表示を切り替える。
  // useHotkeys はこの .active を見て背後の操作を止めるため、状態が正しいことを確かめる
  it("閉じている間は .active が付かない（背後のホットキーを塞がない）", () => {
    mount({}, false);
    expect(document.querySelector(".tui-modal.active")).toBeNull();
  });

  it("開いている間は .active が付く（背後のホットキーを止める）", () => {
    mount();
    expect(document.querySelector(".tui-modal.active")).not.toBeNull();
  });

  it("現在の接続先・設定元・接続状態を表示する", () => {
    mount({ wsUrl: "ws://drc:8080/ws", wsUrlSource: "stored", connected: true });

    expect(screen.getByText("ws://drc:8080/ws")).toBeInTheDocument();
    expect(screen.getByText("設定元: この端末に保存")).toBeInTheDocument();
    expect(screen.getByText("● 接続中")).toBeInTheDocument();
  });

  it("切断中はその旨を出す（接続先違いに気付ける必要がある）", () => {
    mount({ connected: false });
    expect(screen.getByText("● 切断")).toBeInTheDocument();
  });

  it("入力を保存すると setWsUrl に渡る", async () => {
    const user = userEvent.setup();
    const { context } = mount({ wsUrl: "ws://localhost:8080/ws" });

    const input = screen.getByLabelText("接続先");
    await user.clear(input);
    await user.type(input, "drc:8080");
    await user.click(screen.getByRole("button", { name: "保存して再接続" }));

    expect(context.setWsUrl).toHaveBeenCalledWith("drc:8080");
  });

  it("解釈できない入力ならエラーを出す", async () => {
    const user = userEvent.setup();
    mount({ setWsUrl: vi.fn(() => false) });

    await user.click(screen.getByRole("button", { name: "保存して再接続" }));

    expect(screen.getByText(/接続先として解釈できません/)).toBeInTheDocument();
  });

  it("既定に戻すで resetWsUrl を呼ぶ", async () => {
    const user = userEvent.setup();
    const { context } = mount();

    await user.click(screen.getByRole("button", { name: "既定に戻す" }));

    expect(context.resetWsUrl).toHaveBeenCalled();
  });

  it("閉じるで onClose を呼ぶ", async () => {
    const user = userEvent.setup();
    const { onClose } = mount();

    await user.click(screen.getByRole("button", { name: "閉じる" }));

    expect(onClose).toHaveBeenCalled();
  });

  it("dev サーバー経由で開いている時は制御 PC 直結を候補に出す", async () => {
    const user = userEvent.setup();
    // jsdom の hostname は localhost。5173 で開いている想定
    mount({ wsUrl: "ws://localhost:5173/ws" });

    const candidate = screen.getByRole("button", {
      name: "制御 PC へ直結 (ws://localhost:8080/ws)",
    });
    await user.click(candidate);

    expect(screen.getByLabelText("接続先")).toHaveValue("ws://localhost:8080/ws");
  });
});
