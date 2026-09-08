import { act, fireEvent, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { MatchStrip } from "@/components/monitor/MatchControl";
import { RobotProvider } from "@/context/RobotContext";
import { ARM_GUARD_MS, ARM_TIMEOUT_MS } from "@/hooks/useArmedPress";
import type { MatchPhase } from "@/lib/protocol";
import { createRobotContext, DEFAULT_MATCH_STATE, renderWithRobot } from "@/test/robotContext";

/**
 * 試合終了も同じボタンの二度押しで確認を取る（開始と揃えてある）。
 * 試合中に急いで押す操作なので、ダイアログまでカーソルを運ばせない。
 */
describe("MatchStrip の試合終了", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  // fake timer 下では userEvent の内部待ちが解けないため fireEvent を使う
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

  it("セッティングへ戻るは確認を挟まず 1 回で送る", () => {
    // 試合後の唯一の進み先で、失うのは消化済みのチェックリストだけ。
    // 次の試合の準備を 1 クリック遅らせない
    const { view } = mountStrip("finished");

    fireEvent.click(screen.getByRole("button", { name: "セッティングタイムへ戻す" }));

    expect(view.context.matchReset).toHaveBeenCalledTimes(1);
    expect(view.context.matchFinish).not.toHaveBeenCalled();
  });
  it("試合が終わったら武装を持ち越さない", () => {
    // リセットして次の試合へ入ったとき、前の試合で押しかけた 1 回が残っていると
    // 最初の 1 回で試合が終わる
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

/**
 * **武装は押した瞬間の状況に紐づく。** 切断中に押した 1 回目を復帰後の 1 回目と
 * 繋げると、確認なしで `match_finish` が飛ぶ。`StartGate` は最初から `connected` を
 * 武装解除の条件に含めており、ここだけが切断を見ていなかった。
 */
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

/**
 * **EMG STOP の真下に押下可能な要素を置かない。** この帯はヘッダー直下の最上段に出るので、
 * 右端へ寄せると操作ボタンが EMG STOP のほぼ真下（右 16px・下 12px）に来る。誤爆の向きは
 * 「この帯のボタンを狙って外し、緊急停止を踏む」で、試合中に起きればシーケンスが止まる。
 */
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
    // DOM 順だけでは flex 上の見た目の位置は決まらない。並びを先頭にしたまま
    // 右端へ寄せ直せてしまうので、右寄せの指定が無いことも併せて見る
    expect(band.className).not.toMatch(/justify-(end|between)/);
  });

  it("武装して説明文が出てもボタンの位置が動かない", () => {
    // 説明文が左にあると、押した瞬間に文が現れたぶんボタンが横へずれ、
    // 二度押しの 2 回目が 1 回目と違う場所になる
    const { band } = mountStrip();

    fireEvent.click(screen.getByRole("button", { name: "試合を終了する" }));

    const button = screen.getByRole("button", { name: "もう一度押して試合を終了する" });
    const note = screen.getByText(/緊急停止ではありません/);
    const children = Array.from(band.children);
    expect(band.firstElementChild).toBe(button);
    expect(children.indexOf(button)).toBeLessThan(children.indexOf(note));
  });

  it("試合中と試合終了後でボタンの位置が変わらない", () => {
    // 同じ場所へ交互に出るものなので、フェーズで位置が変わると押す直前に探し直しになる
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
