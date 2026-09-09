import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { EStopOverlay } from "@/components/shell/EStopOverlay";
import { DEFAULT_SERVER_INFO, renderWithRobot } from "@/test/robotContext";

const HIDE_BUTTON = "緊急停止ダイアログを開発用に非表示にする";

describe("EStopOverlay", () => {
  it("停止していなければ何も出さない", () => {
    renderWithRobot(<EStopOverlay />, { eStopActive: false });
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
  });

  it("サーバーが載せた停止理由を出す", () => {
    renderWithRobot(<EStopOverlay />, {
      eStopActive: true,
      eStopReason: "同期ずれを検知しました (y_axis)",
    });

    expect(screen.getByText(/同期ずれを検知しました \(y_axis\)/)).toBeInTheDocument();
  });

  it("理由が無い停止 (操縦者コマンド) ではその旨を出す", () => {
    renderWithRobot(<EStopOverlay />, { eStopActive: true, eStopReason: null });

    expect(screen.getByRole("alertdialog")).toBeInTheDocument();
    expect(screen.getByText(/操縦者の停止操作/)).toBeInTheDocument();
  });

  it("非表示にしている間は停止中でも出さない", () => {
    renderWithRobot(<EStopOverlay />, { eStopActive: true, eStopOverlayHidden: true });
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
  });

  describe("開発用の非表示ボタン", () => {
    it("本番 (dev_tools 無効) では出さない", () => {
      renderWithRobot(<EStopOverlay />, { eStopActive: true });

      expect(screen.getByRole("alertdialog")).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: HIDE_BUTTON })).not.toBeInTheDocument();
    });

    it("dev_tools 有効なら出し、押すと非表示を要求する", async () => {
      const user = userEvent.setup();
      const { context } = renderWithRobot(<EStopOverlay />, {
        eStopActive: true,
        serverInfo: { ...DEFAULT_SERVER_INFO, dev_tools: true },
      });

      await user.click(screen.getByRole("button", { name: HIDE_BUTTON }));

      expect(context.hideEStopOverlay).toHaveBeenCalled();
    });

    it("Reset は dev_tools の有無にかかわらず残る", () => {
      renderWithRobot(<EStopOverlay />, {
        eStopActive: true,
        serverInfo: { ...DEFAULT_SERVER_INFO, dev_tools: true },
      });

      expect(screen.getByRole("button", { name: /Reset/ })).toBeInTheDocument();
    });
  });
});
