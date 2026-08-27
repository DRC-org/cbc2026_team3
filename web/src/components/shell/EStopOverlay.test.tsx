import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { EStopOverlay } from "@/components/shell/EStopOverlay";
import { renderWithRobot } from "@/test/robotContext";

describe("EStopOverlay", () => {
  it("停止していなければ何も出さない", () => {
    renderWithRobot(<EStopOverlay />, { eStopActive: false });
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
  });

  it("サーバーが載せた停止理由を出す", () => {
    // 最も危険なのは SyncMonitor による自動停止。理由が出ないと操縦者は
    // 「誰かが押したのか、機体が壊れたのか」を画面から区別できず、復旧手順を選べない
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
});
