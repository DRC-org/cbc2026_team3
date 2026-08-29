import { act, fireEvent, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { MatchSettings, MatchStrip } from "@/components/monitor/MatchControl";
import { RobotProvider } from "@/context/RobotContext";
import { ARM_GUARD_MS, ARM_TIMEOUT_MS } from "@/hooks/useArmedPress";
import type { MatchPhase } from "@/lib/protocol";
import { createRobotContext, DEFAULT_MATCH_STATE, renderWithRobot } from "@/test/robotContext";

function mount(phase: MatchPhase = "setup") {
  return renderWithRobot(<MatchSettings onRequestReset={vi.fn()} />, {
    matchState: { ...DEFAULT_MATCH_STATE, phase, court: "red" },
  });
}

/**
 * コート選択は「今どちらか」を色で示す唯一の場所 (誤ったコートのまま試合に入る事故は
 * 試合をそのまま落とす)。選択中の面はコートの色そのものでなければ意味を成さないため、
 * 汎用の反転表示 (`Button` の generic な selected) ではなく呼び出し側が色を持つ。
 */
describe("MatchSettings のコート選択", () => {
  it("選択中のコートを色付きの面と aria-pressed で示す", () => {
    mount();

    const red = screen.getByRole("button", { name: "赤コート" });
    const blue = screen.getByRole("button", { name: "青コート" });

    expect(red).toHaveAttribute("aria-pressed", "true");
    expect(red).toHaveClass("bg-error");
    expect(blue).toHaveAttribute("aria-pressed", "false");
    expect(blue).not.toHaveClass("bg-info");
  });

  it("選択されていないコートを押すと set_court を送る", async () => {
    const { context } = mount();

    await userEvent.click(screen.getByRole("button", { name: "青コート" }));

    expect(context.setCourt).toHaveBeenCalledWith("blue");
  });

  it("試合中はコートを変更させない (サーバーも同じフェーズで拒否する)", () => {
    mount("match");

    expect(screen.getByRole("button", { name: "赤コート" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "青コート" })).toBeDisabled();
  });
});

/**
 * 試合終了も同じボタンの二度押しで確認を取る（開始と揃えてある）。
 * 試合中に急いで押す操作なので、ダイアログまでカーソルを運ばせない。
 */
describe("MatchStrip の試合終了", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  // fake timer 下では userEvent の内部待ちが解けないため fireEvent を使う
  function mountStrip(phase: MatchPhase = "match") {
    const onRequestReset = vi.fn();
    const view = renderWithRobot(<MatchStrip onRequestReset={onRequestReset} />, {
      matchState: { ...DEFAULT_MATCH_STATE, phase, court: "red" },
    });
    return { onRequestReset, view };
  }

  const finishButton = () => screen.getByRole("button", { name: /試合を終了する/ });

  it("1 回目では終了せず、ボタン自身が確認を求める", () => {
    const { view } = mountStrip();

    fireEvent.click(screen.getByRole("button", { name: "試合を終了する" }));

    expect(view.context.matchFinish).not.toHaveBeenCalled();
    expect(
      screen.getByRole("button", { name: "もう一度押して試合を終了する" }),
    ).toBeInTheDocument();
    // ダイアログが持っていた「緊急停止ではない」ことはここへ移してある
    expect(screen.getByText(/緊急停止ではありません/)).toBeInTheDocument();
  });

  it("不感時間を過ぎた 2 回目で match_finish を送る", () => {
    const { view } = mountStrip();

    fireEvent.click(finishButton());
    act(() => vi.advanceTimersByTime(ARM_GUARD_MS));
    fireEvent.click(finishButton());

    expect(view.context.matchFinish).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "試合を終了する" })).toBeInTheDocument();
  });

  it("ダブルクリック 1 回では終了しない", () => {
    const { view } = mountStrip();

    // 時間を進めずに 2 発。物理的なダブルクリックはこの形で届く
    fireEvent.click(finishButton());
    fireEvent.click(finishButton());

    expect(view.context.matchFinish).not.toHaveBeenCalled();
  });

  it("放置すると確認が解け、次の 1 回はまた 1 回目になる", () => {
    const { view } = mountStrip();

    fireEvent.click(finishButton());
    act(() => vi.advanceTimersByTime(ARM_TIMEOUT_MS));
    expect(screen.queryByText(/緊急停止ではありません/)).not.toBeInTheDocument();

    fireEvent.click(finishButton());
    expect(view.context.matchFinish).not.toHaveBeenCalled();
  });

  it("試合が終わっていればリセット導線を出す（こちらはダイアログのまま）", () => {
    const { onRequestReset, view } = mountStrip("finished");

    fireEvent.click(screen.getByRole("button", { name: "セッティングタイムへ戻す" }));

    expect(onRequestReset).toHaveBeenCalledTimes(1);
    expect(view.context.matchFinish).not.toHaveBeenCalled();
  });
  it("試合が終わったら武装を持ち越さない", () => {
    // リセットして次の試合へ入ったとき、前の試合で押しかけた 1 回が残っていると
    // 最初の 1 回で試合が終わる
    const view = renderWithRobot(<MatchStrip onRequestReset={vi.fn()} />, {
      matchState: { ...DEFAULT_MATCH_STATE, phase: "match", court: "red" },
    });

    fireEvent.click(finishButton());
    act(() => vi.advanceTimersByTime(ARM_GUARD_MS));

    const rerenderWith = (phase: MatchPhase) =>
      view.rerender(
        <RobotProvider
          value={createRobotContext({
            ...view.context,
            matchState: { ...DEFAULT_MATCH_STATE, phase, court: "red" },
          })}
        >
          <MatchStrip onRequestReset={vi.fn()} />
        </RobotProvider>,
      );

    rerenderWith("finished");
    rerenderWith("match");

    fireEvent.click(finishButton());
    expect(view.context.matchFinish).not.toHaveBeenCalled();
  });
});
