import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { HomingButtons } from "@/components/homing/HomingButtons";
import { MALFORMED } from "@/lib/protocol";
import type { HomingSnapshot } from "@/lib/protocol";
import { EMPTY_HOMING, renderWithRobot } from "@/test/robotContext";

const TARGETS = { main_hand: ["y_axis", "rotate"], sub_hand: ["sub_y_axis"] };

function mount(homing: Partial<HomingSnapshot> = {}, extra: Record<string, unknown> = {}) {
  return renderWithRobot(<HomingButtons />, {
    connected: true,
    homing: {
      ...EMPTY_HOMING,
      available: true,
      blocked_reason: null,
      targets: TARGETS,
      ...homing,
    },
    ...extra,
  });
}

const SUB_BUTTON = { name: "サブハンドの零点合わせを開始" };

describe("HomingButtons", () => {
  it("ロボットごとにボタンを出し、宛先の軸を押す前に見せる", () => {
    mount();

    expect(screen.getByRole("button", SUB_BUTTON)).toBeEnabled();
    expect(screen.getByRole("button", { name: "メインハンドの零点合わせを開始" })).toBeEnabled();
    expect(screen.getByText("sub_y_axis")).toBeInTheDocument();
    expect(screen.getByText("y_axis, rotate")).toBeInTheDocument();
  });

  it("確認してから、押したロボットの軸だけを送る", async () => {
    const sendOrReport = vi.fn(() => true);
    mount({}, { sendOrReport });

    await userEvent.click(screen.getByRole("button", SUB_BUTTON));
    await userEvent.click(screen.getByRole("button", { name: "開始" }));

    expect(sendOrReport).toHaveBeenCalledWith(
      { type: "homing_start", robot: "sub_hand", axes: ["sub_y_axis"] },
      "零点合わせの開始",
    );
  });

  it("確認に宛先のロボット名が出る", async () => {
    mount();

    await userEvent.click(screen.getByRole("button", SUB_BUTTON));

    expect(screen.getByText("サブハンドだけ")).toBeInTheDocument();
  });

  it("キャンセルしたら送らない", async () => {
    const sendOrReport = vi.fn(() => true);
    mount({}, { sendOrReport });

    await userEvent.click(screen.getByRole("button", SUB_BUTTON));
    await userEvent.click(screen.getByRole("button", { name: "キャンセル" }));

    expect(sendOrReport).not.toHaveBeenCalled();
  });

  it("塞ぐ理由はサーバーの blocked_reason をそのまま出す", () => {
    mount({ blocked_reason: "動作確認の実行中は零点合わせを実行できません" });

    expect(screen.getByRole("button", SUB_BUTTON)).toBeDisabled();
    expect(screen.getByText("動作確認の実行中は零点合わせを実行できません")).toBeInTheDocument();
  });

  it("切断中は押せない", () => {
    renderWithRobot(<HomingButtons />, {
      connected: false,
      homing: { ...EMPTY_HOMING, available: true, blocked_reason: null, targets: TARGETS },
    });

    expect(screen.getByRole("button", SUB_BUTTON)).toBeDisabled();
  });

  it("robot を絞ると、その機体のボタンだけ出す", () => {
    renderWithRobot(<HomingButtons robot="sub_hand" />, {
      connected: true,
      homing: { ...EMPTY_HOMING, available: true, blocked_reason: null, targets: TARGETS },
    });

    expect(screen.getByRole("button", SUB_BUTTON)).toBeEnabled();
    expect(screen.queryByRole("button", { name: "メインハンドの零点合わせを開始" })).toBeNull();
  });

  it("絞った先に対象が無ければボタンを出さず知らせる", () => {
    renderWithRobot(<HomingButtons robot="sub_hand" />, {
      connected: true,
      homing: {
        ...EMPTY_HOMING,
        available: true,
        blocked_reason: null,
        targets: { main_hand: ["y_axis"] },
      },
    });

    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.getByText("零点確定できる軸がありません。")).toBeInTheDocument();
  });

  it("対象を読み取れなければボタンを出さずに知らせる", () => {
    mount({ targets: MALFORMED });

    expect(screen.queryByRole("button", SUB_BUTTON)).not.toBeInTheDocument();
    expect(screen.getByText(/読み取れませんでした/)).toBeInTheDocument();
  });
});
