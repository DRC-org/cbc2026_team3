import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { HomingButtons } from "@/components/homing/HomingButtons";
import { MALFORMED } from "@/lib/protocol";
import type { HomingSnapshot, RobotState } from "@/lib/protocol";
import { EMPTY_HOMING, renderWithRobot } from "@/test/robotContext";

const TARGETS = { main_hand: ["y_axis", "rotate"], sub_hand: ["sub_y_axis"] };

function mount(homing: Partial<HomingSnapshot> = {}, extra: Record<string, unknown> = {}) {
  return renderWithRobot(<HomingButtons robot="sub_hand" />, {
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

function robotIn(mode: "sequence" | "manual"): RobotState {
  return { manual: { mode, axes: [] } } as unknown as RobotState;
}

const MANUAL_STATES = { main_hand: robotIn("manual"), sub_hand: robotIn("sequence") };
const MANUAL_REASON = "'main_hand' が手動操縦モードのため零点合わせを実行できません";

describe("HomingButtons", () => {
  it("担当機のボタンだけを出し、宛先の軸を押す前に見せる", () => {
    mount();

    expect(screen.getByRole("button", SUB_BUTTON)).toBeEnabled();
    expect(screen.queryByRole("button", { name: "メインハンドの零点合わせを開始" })).toBeNull();
    expect(screen.getByText("sub_y_axis")).toBeInTheDocument();
    expect(screen.queryByText("y_axis, rotate")).toBeNull();
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
    renderWithRobot(<HomingButtons robot="sub_hand" />, {
      connected: false,
      homing: { ...EMPTY_HOMING, available: true, blocked_reason: null, targets: TARGETS },
    });

    expect(screen.getByRole("button", SUB_BUTTON)).toBeDisabled();
  });

  it("担当機に対象が無ければボタンを出さず知らせる", () => {
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

  describe("手動操縦中", () => {
    it("サーバーの拒否理由では塞がず、確認に半自動へ戻す旨を出す", async () => {
      mount({ blocked_reason: MANUAL_REASON }, { states: MANUAL_STATES });

      expect(screen.getByRole("button", SUB_BUTTON)).toBeEnabled();
      expect(screen.queryByText(MANUAL_REASON)).toBeNull();

      await userEvent.click(screen.getByRole("button", SUB_BUTTON));

      expect(screen.getByText(/全機を半自動へ戻してから開始/)).toBeInTheDocument();
    });

    it("全機を半自動へ戻してから零点合わせを送る", async () => {
      const sendOrReport = vi.fn<(data: unknown, what: string) => boolean>(() => true);
      mount({ blocked_reason: MANUAL_REASON }, { states: MANUAL_STATES, sendOrReport });

      await userEvent.click(screen.getByRole("button", SUB_BUTTON));
      await userEvent.click(screen.getByRole("button", { name: "開始" }));

      expect(sendOrReport.mock.calls.map(([data]) => data)).toEqual([
        { type: "set_operation_mode", robot: "main_hand", mode: "sequence" },
        { type: "set_operation_mode", robot: "sub_hand", mode: "sequence" },
        { type: "homing_start", robot: "sub_hand", axes: ["sub_y_axis"] },
      ]);
    });

    it("半自動へ戻せなければ零点合わせを送らない", async () => {
      const sendOrReport = vi.fn(() => false);
      mount({ blocked_reason: MANUAL_REASON }, { states: MANUAL_STATES, sendOrReport });

      await userEvent.click(screen.getByRole("button", SUB_BUTTON));
      await userEvent.click(screen.getByRole("button", { name: "開始" }));

      expect(sendOrReport).not.toHaveBeenCalledWith(
        expect.objectContaining({ type: "homing_start" }),
        expect.any(String),
      );
    });

    it("切断中・緊急停止中は手動でも塞ぐ", () => {
      const view = mount(
        { blocked_reason: "緊急停止中のため零点合わせを実行できません" },
        { states: MANUAL_STATES, eStopActive: true },
      );
      expect(screen.getByRole("button", SUB_BUTTON)).toBeDisabled();
      expect(screen.getByText("緊急停止中のため零点合わせを実行できません")).toBeInTheDocument();
      view.unmount();

      mount({ blocked_reason: MANUAL_REASON }, { states: MANUAL_STATES, connected: false });
      expect(screen.getByRole("button", SUB_BUTTON)).toBeDisabled();
      expect(screen.getByText("切断中のため不可")).toBeInTheDocument();
    });

    it("全機が半自動なら確認に半自動へ戻す旨を出さない", async () => {
      mount({}, { states: { sub_hand: robotIn("sequence") } });

      await userEvent.click(screen.getByRole("button", SUB_BUTTON));

      expect(screen.queryByText(/半自動へ戻してから/)).toBeNull();
    });
  });
});
