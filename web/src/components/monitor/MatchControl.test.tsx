import { act, fireEvent, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { MatchStrip } from "@/components/monitor/MatchControl";
import { RobotProvider } from "@/context/RobotContext";
import { ARM_GUARD_MS, ARM_TIMEOUT_MS } from "@/hooks/useArmedPress";
import type { MatchPhase, MatchTimer } from "@/lib/protocol";
import { createRobotContext, DEFAULT_MATCH_STATE, renderWithRobot } from "@/test/robotContext";

describe("MatchStrip の試合終了", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  function mountStrip(phase: MatchPhase = "match", connected = true) {
    const view = renderWithRobot(<MatchStrip />, {
      connected,
      matchState: { ...DEFAULT_MATCH_STATE, phase, court: "red" },
    });
    return { view };
  }

  const finishButton = () => screen.getByRole("button", { name: /試合を終了する/ });

  it("1 回目では終了せず、ボタン自身が確認を求める", () => {
    const { view } = mountStrip();

    // fake timer 下では userEvent の内部待ちが解けないため fireEvent を使う
    fireEvent.click(screen.getByRole("button", { name: "試合を終了する" }));

    expect(view.context.matchFinish).not.toHaveBeenCalled();
    expect(
      screen.getByRole("button", { name: "もう一度押して試合を終了する" }),
    ).toBeInTheDocument();
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

  it("セッティングへ戻るは確認を挟まず 1 回で送る", () => {
    const { view } = mountStrip("finished");

    fireEvent.click(screen.getByRole("button", { name: "セッティングタイムへ戻す" }));

    expect(view.context.matchReset).toHaveBeenCalledTimes(1);
    expect(view.context.matchFinish).not.toHaveBeenCalled();
  });
  it("試合が終わったら武装を持ち越さない", () => {
    const view = renderWithRobot(<MatchStrip />, {
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
          <MatchStrip />
        </RobotProvider>,
      );

    rerenderWith("finished");
    rerenderWith("match");

    fireEvent.click(finishButton());
    expect(view.context.matchFinish).not.toHaveBeenCalled();
  });
});

describe("MatchStrip の切断中", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  const finishButton = () => screen.getByRole("button", { name: /試合を終了する|操作不可/ });

  it("切断中は試合終了を押せず、理由を出す", () => {
    renderWithRobot(<MatchStrip />, {
      connected: false,
      matchState: { ...DEFAULT_MATCH_STATE, phase: "match", court: "red" },
    });

    expect(screen.getByRole("button", { name: /操作不可/ })).toBeDisabled();
    expect(screen.getByText("切断中")).toBeInTheDocument();
  });

  it("切断を跨いだ 2 回目を「確認済み」として扱わない", () => {
    const view = renderWithRobot(<MatchStrip />, {
      connected: true,
      matchState: { ...DEFAULT_MATCH_STATE, phase: "match", court: "red" },
    });

    fireEvent.click(finishButton());
    act(() => vi.advanceTimersByTime(ARM_GUARD_MS));

    const rerenderWith = (connected: boolean) =>
      view.rerender(
        <RobotProvider
          value={createRobotContext({
            ...view.context,
            connected,
            matchState: { ...DEFAULT_MATCH_STATE, phase: "match", court: "red" },
          })}
        >
          <MatchStrip />
        </RobotProvider>,
      );

    rerenderWith(false);
    rerenderWith(true);

    fireEvent.click(finishButton());
    expect(view.context.matchFinish).not.toHaveBeenCalled();
  });

  it("切断中はセッティングへ戻るも押せない", () => {
    renderWithRobot(<MatchStrip />, {
      connected: false,
      matchState: { ...DEFAULT_MATCH_STATE, phase: "finished", court: "red" },
    });

    expect(screen.getByRole("button", { name: /操作不可/ })).toBeDisabled();
  });
});

describe("MatchStrip の操作ボタンの位置", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  function mountStrip(phase: MatchPhase = "match") {
    const view = renderWithRobot(<MatchStrip />, {
      matchState: { ...DEFAULT_MATCH_STATE, phase, court: "red" },
    });
    return { view, band: view.container.firstElementChild as HTMLElement };
  }

  it("操作ボタンを帯の右端に置かない", () => {
    const { band } = mountStrip();

    expect(band.firstElementChild).toBe(screen.getByRole("button", { name: "試合を終了する" }));
    expect(band.className).not.toMatch(/justify-(end|between)/);
  });

  it("武装して説明文が出てもボタンの位置が動かない", () => {
    const { band } = mountStrip();

    fireEvent.click(screen.getByRole("button", { name: "試合を終了する" }));

    const button = screen.getByRole("button", { name: "もう一度押して試合を終了する" });
    const note = screen.getByText(/緊急停止ではありません/);
    const children = Array.from(band.children);
    expect(band.firstElementChild).toBe(button);
    expect(children.indexOf(button)).toBeLessThan(children.indexOf(note));
  });

  it("試合中と試合終了後でボタンの位置が変わらない", () => {
    const during = mountStrip("match");
    expect(during.band.firstElementChild).toBe(
      screen.getByRole("button", { name: "試合を終了する" }),
    );
    during.view.unmount();

    const after = mountStrip("finished");
    expect(after.band.firstElementChild).toBe(
      screen.getByRole("button", { name: "セッティングタイムへ戻す" }),
    );
  });
});

describe("MatchStrip の残り時間", () => {
  let perfNow = 0;

  beforeEach(() => {
    vi.useFakeTimers();
    perfNow = 0;
    vi.spyOn(performance, "now").mockImplementation(() => perfNow);
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  function mountWithTimer(timer: MatchTimer | null, phase: MatchPhase = "match") {
    return renderWithRobot(<MatchStrip />, {
      connected: true,
      matchState: { ...DEFAULT_MATCH_STATE, phase, court: "red", timer },
    });
  }

  it("試合中は残り時間を出す", () => {
    mountWithTimer({ running: true, elapsed_ms: 60_000, duration_ms: 180_000 });

    expect(screen.getByText("残り")).toBeInTheDocument();
    expect(screen.getByText("2:00")).toBeInTheDocument();
  });

  it("試合終了後は凍結した値を「終了時点」として出し続ける", () => {
    mountWithTimer({ running: false, elapsed_ms: 150_000, duration_ms: 180_000 }, "finished");

    expect(screen.getByText("終了時点")).toBeInTheDocument();
    expect(screen.getByText("0:30")).toBeInTheDocument();

    act(() => {
      perfNow += 5_000;
      vi.advanceTimersByTime(5_000);
    });

    expect(screen.getByText("0:30")).toBeInTheDocument();
  });

  it("未受信では時計を出さない (0:00 を確信して出さない)", () => {
    mountWithTimer(null);

    expect(screen.queryByText("残り")).not.toBeInTheDocument();
    expect(screen.queryByText("0:00")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "試合を終了する" })).toBeInTheDocument();
  });
});
