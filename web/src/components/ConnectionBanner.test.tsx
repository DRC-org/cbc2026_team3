import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { ConnectionBanner } from "@/components/ConnectionBanner";
import { renderWithRobot } from "@/test/robotContext";

describe("ConnectionBanner", () => {
  it("接続中は何も出さない", () => {
    renderWithRobot(<ConnectionBanner />, { connected: true });
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("切断中は現在の接続先を出し、そこから設定を開ける", async () => {
    const user = userEvent.setup();
    const { context } = renderWithRobot(<ConnectionBanner />, {
      connected: false,
      wsUrl: "ws://drc:8080/ws",
    });

    expect(screen.getByRole("alert")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "接続先 ws://drc:8080/ws を変更" }));

    expect(context.openWsSettings).toHaveBeenCalled();
  });
});
