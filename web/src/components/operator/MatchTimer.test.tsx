import { act, render, screen } from "@testing-library/react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { MatchTimer } from "@/components/operator/MatchTimer";
import type { MatchTimer as MatchTimerValue } from "@/lib/protocol";
import { formatRemaining } from "@/lib/time";

const DURATION_MS = 180_000;

let perfNow = 0;

function advance(ms: number): void {
  act(() => {
    perfNow += ms;
    vi.advanceTimersByTime(ms);
  });
}

function timerValue(overrides: Partial<MatchTimerValue> = {}): MatchTimerValue {
  return { running: true, elapsed_ms: 0, duration_ms: DURATION_MS, ...overrides };
}

function displayed(container: HTMLElement): string {
  return container.querySelector(".font-mono")?.textContent ?? "";
}

beforeEach(() => {
  vi.useFakeTimers();
  perfNow = 0;
  vi.spyOn(performance, "now").mockImplementation(() => perfNow);
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("formatRemaining", () => {
  it("残りを M:SS で表す", () => {
    expect(formatRemaining(180_000)).toBe("3:00");
    expect(formatRemaining(65_000)).toBe("1:05");
    expect(formatRemaining(0)).toBe("0:00");
  });

  it("端数は切り上げる — 0:00 は本当に時間が尽きたときだけ出す", () => {
    expect(formatRemaining(900)).toBe("0:01");
    expect(formatRemaining(1)).toBe("0:01");
  });
});

describe("MatchTimer", () => {
  it("ページを長く開いた後にマウントしても、最初の 1 フレームから正しい残りを描く", () => {
    perfNow = 600_000;

    const html = renderToStaticMarkup(<MatchTimer timer={timerValue({ elapsed_ms: 30_000 })} />);

    expect(html).toContain("2:30");
    expect(html).not.toContain("0:00");
  });

  it("試合中は自分の時計で残り時間を減らす", () => {
    const { container } = render(<MatchTimer timer={timerValue()} />);
    expect(displayed(container)).toBe("3:00");

    advance(45_000);
    expect(displayed(container)).toBe("2:15");
  });

  it("サーバーの経過値を起点にする — 途中接続でも 0 から数え直さない", () => {
    const { container } = render(<MatchTimer timer={timerValue({ elapsed_ms: 100_000 })} />);

    expect(displayed(container)).toBe("1:20");
  });

  it("別々の時刻にマウントした 2 台が同じ残り時間を出す", () => {
    const first = render(<MatchTimer timer={timerValue({ elapsed_ms: 20_000 })} />);

    advance(30_000);

    const second = render(<MatchTimer timer={timerValue({ elapsed_ms: 50_000 })} />);

    expect(displayed(second.container)).toBe(displayed(first.container));
    expect(displayed(first.container)).toBe("2:10");

    advance(10_000);
    expect(displayed(second.container)).toBe(displayed(first.container));
    expect(displayed(first.container)).toBe("2:00");
  });

  it("新しい配信が届いたらアンカーを取り直す", () => {
    const { container, rerender } = render(<MatchTimer timer={timerValue()} />);
    advance(10_000);
    expect(displayed(container)).toBe("2:50");

    rerender(<MatchTimer timer={timerValue({ elapsed_ms: 60_000 })} />);
    expect(displayed(container)).toBe("2:00");

    advance(5_000);
    expect(displayed(container)).toBe("1:55");
  });

  it("0:00 で止まり、マイナスにならない", () => {
    const { container } = render(<MatchTimer timer={timerValue()} />);

    advance(DURATION_MS + 30_000);
    expect(displayed(container)).toBe("0:00");
  });

  it("running が false なら進めない — 試合終了時点の値で凍る", () => {
    const frozen = timerValue({ running: false, elapsed_ms: 150_000 });
    const { container, rerender } = render(<MatchTimer timer={frozen} />);
    expect(displayed(container)).toBe("0:30");

    advance(20_000);
    rerender(<MatchTimer timer={{ ...frozen }} />);

    expect(displayed(container)).toBe("0:30");
    expect(screen.getByText("試合終了時点")).toBeInTheDocument();
  });

  it("開始前は満了時間を出し、終了後と文言で区別する", () => {
    render(<MatchTimer timer={timerValue({ running: false, elapsed_ms: 0 })} />);

    expect(screen.getByText("開始前")).toBeInTheDocument();
  });

  it("進行中は caption を持たず、停止中だけが caption を持つ", () => {
    const { container, rerender } = render(<MatchTimer timer={timerValue()} />);
    const captions = () =>
      Array.from(container.querySelectorAll("span")).filter(
        (el) => !el.className.includes("font-mono"),
      );

    expect(captions()).toHaveLength(0);

    rerender(<MatchTimer timer={timerValue({ running: false, elapsed_ms: 150_000 })} />);
    expect(captions().map((el) => el.textContent)).toEqual(["試合終了時点"]);
  });

  it("タイマーが読めなければ数字を出さず、読めていないことを言う", () => {
    render(<MatchTimer timer={null} />);

    expect(screen.getByText("タイマー未受信")).toBeInTheDocument();
  });

  it("秒の切り替わりちょうどに起床する — デバイス間で繰り上がりがずれない", () => {
    const { container } = render(<MatchTimer timer={timerValue({ elapsed_ms: 100 })} />);
    expect(displayed(container)).toBe("3:00");

    advance(899);
    expect(displayed(container)).toBe("3:00");

    advance(21);
    expect(displayed(container)).toBe("2:59");
  });
});
