import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { EStopBanner } from "@/components/shell/EStopBanner";
import { renderWithRobot } from "@/test/robotContext";

describe("EStopBanner", () => {
  it("停止していなければ何も出さない", () => {
    renderWithRobot(<EStopBanner />, { eStopActive: false, eStopOverlayHidden: true });
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("停止中でもオーバーレイが出ているうちは何も出さない", () => {
    renderWithRobot(<EStopBanner />, { eStopActive: true, eStopOverlayHidden: false });
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("オーバーレイを隠している停止中だけ、停止理由とともに出る", () => {
    renderWithRobot(<EStopBanner />, {
      eStopActive: true,
      eStopOverlayHidden: true,
      eStopReason: "同期ずれを検知しました (y_axis)",
    });

    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(screen.getByText(/EMERGENCY STOP/)).toBeInTheDocument();
    expect(screen.getByText(/同期ずれを検知しました \(y_axis\)/)).toBeInTheDocument();
  });

  it("理由が無い停止 (操縦者コマンド) ではその旨を出す", () => {
    renderWithRobot(<EStopBanner />, {
      eStopActive: true,
      eStopOverlayHidden: true,
      eStopReason: null,
    });

    expect(screen.getByText(/操縦者の停止操作/)).toBeInTheDocument();
  });

  it("帯からも解除できる (解除手段が画面から消えない)", async () => {
    const user = userEvent.setup();
    const { context } = renderWithRobot(<EStopBanner />, {
      eStopActive: true,
      eStopOverlayHidden: true,
    });

    await user.click(screen.getByRole("button", { name: /Reset/ }));

    expect(context.onEStopRelease).toHaveBeenCalled();
  });
});
