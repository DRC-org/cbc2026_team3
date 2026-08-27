import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { MatchTimer, formatRemaining } from "@/components/operator/MatchTimer";
import type { MatchTimer as MatchTimerValue } from "@/lib/protocol";

/**
 * タイマーは「全デバイスで同じ値が出る」ことが存在理由なので、ここでは表示より先に
 * **同期の性質**を固定する。ずれた場合、症状は 1 台だけの異常ではなく
 * 「画面によって残り時間が違う」という形で現れ、どれが正しいのか誰にも分からない。
 *
 * 実時間に依存させると CI の負荷で揺れる試験になるため、単調時計 (`performance.now`)
 * ごとスタブして進める。
 */

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
    // 切り捨てにすると残り 0.9 秒でも 0:00 になり、まだ動かせる時間を捨てさせる
    expect(formatRemaining(900)).toBe("0:01");
    expect(formatRemaining(1)).toBe("0:01");
  });
});

describe("MatchTimer", () => {
  it("試合中は自分の時計で残り時間を減らす", () => {
    const { container } = render(<MatchTimer timer={timerValue()} />);
    expect(displayed(container)).toBe("3:00");

    advance(45_000);
    expect(displayed(container)).toBe("2:15");
  });

  it("サーバーの経過値を起点にする — 途中接続でも 0 から数え直さない", () => {
    // リロードした操縦者・後から開いた Monitor がここを踏む。配信された経過を
    // 無視して 0 から数えると、その端末だけ残り時間が長く出る
    const { container } = render(<MatchTimer timer={timerValue({ elapsed_ms: 100_000 })} />);

    expect(displayed(container)).toBe("1:20");
  });

  it("別々の時刻にマウントした 2 台が同じ残り時間を出す", () => {
    // これがこの部品の存在理由そのもの。壁時計ではなく配信された経過を起点に
    // するため、端末の時計が揃っていなくても一致する
    const first = render(<MatchTimer timer={timerValue({ elapsed_ms: 20_000 })} />);

    advance(30_000);

    // 30 秒後に別デバイスが接続し、その時点のサーバー値 (20s + 30s) を受け取る
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

    // サーバー側が真とみなす値へ引き戻される (端末の時計がずれていた場合)
    rerender(<MatchTimer timer={timerValue({ elapsed_ms: 60_000 })} />);
    expect(displayed(container)).toBe("2:00");

    advance(5_000);
    expect(displayed(container)).toBe("1:55");
  });

  it("0:00 で止まり、マイナスにならない", () => {
    // 表示のみの設計 (試合終了はあくまで操縦者の match_finish)。
    // マイナス表示は競技時計として意味を持たない
    const { container } = render(<MatchTimer timer={timerValue()} />);

    advance(DURATION_MS + 30_000);
    expect(displayed(container)).toBe("0:00");
  });

  it("running が false なら進めない — 試合終了時点の値で凍る", () => {
    // 進み続けると「何秒残して終えたのか」が結果確認の時点で読めなくなる
    const frozen = timerValue({ running: false, elapsed_ms: 150_000 });
    const { container, rerender } = render(<MatchTimer timer={frozen} />);
    expect(displayed(container)).toBe("0:30");

    advance(20_000);
    // 停止中は自分では再描画しないので、時間だけ進めても値が動かないのは当然。
    // 他の要因で再描画が起きたときに進んでいないことまで見ないと、
    // 「停止中も自分の時計で進める」実装を素通しさせてしまう
    rerender(<MatchTimer timer={{ ...frozen }} />);

    expect(displayed(container)).toBe("0:30");
    expect(screen.getByText("試合終了時点の残り")).toBeInTheDocument();
  });

  it("開始前は満了時間を出し、終了後と文言で区別する", () => {
    render(<MatchTimer timer={timerValue({ running: false, elapsed_ms: 0 })} />);

    expect(screen.getByText("開始前")).toBeInTheDocument();
  });

  it("タイマーが読めなければ数字を出さず、読めていないことを言う", () => {
    // 誤った数字を自信満々に出すより、読めていないと言うほうが操縦者の判断に資する
    render(<MatchTimer timer={null} />);

    expect(screen.getByText("タイマー未受信")).toBeInTheDocument();
  });

  it("秒の切り替わりちょうどに起床する — デバイス間で繰り上がりがずれない", () => {
    // 固定間隔で起こすと端末ごとに起床位相がずれ、同じ値を持っているのに
    // 秒の繰り上がりが最大 1 周期ぶん食い違って見える。
    // 起点を 100ms ずらして境界を「固定間隔では踏めない位置」へ置く
    const { container } = render(<MatchTimer timer={timerValue({ elapsed_ms: 100 })} />);
    expect(displayed(container)).toBe("3:00");

    // 経過 1000ms (= 残り 179_000ms) を割った瞬間に 2:59 へ変わる
    advance(899);
    expect(displayed(container)).toBe("3:00");

    // 境界を 20ms 過ぎただけで表示が変わること。固定間隔 (例: 250ms) だと
    // 次の起床は経過 1000ms 以降になり、ここではまだ 3:00 のままになる
    advance(21);
    expect(displayed(container)).toBe("2:59");
  });
});
