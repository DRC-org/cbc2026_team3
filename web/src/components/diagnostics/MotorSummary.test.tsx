import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { MotorSummary } from "@/components/diagnostics/MotorSummary";
import type { MotorHealth, MotorState } from "@/lib/protocol";
import { motorState } from "@/test/motorState";

/** 温度は常に正常域。サマリーが温度ではなくヘルスを見ていることを確かめるため */
const MOTORS: Record<string, MotorState> = {
  y_axis_r: motorState(),
  y_axis_l: motorState(),
};

function motorHealth(over: Partial<MotorHealth> = {}): MotorHealth {
  return {
    name: "y_axis_r",
    bus: "can_m3508",
    state: "ok",
    last_feedback_at: null,
    feedback_age_ms: 0,
    temperature: 30,
    detail: null,
    ...over,
  };
}

/** 見出しチップ (モータ基数の隣に出る 1 つ目のバッジ) */
function verdictBadge(): HTMLElement {
  const badge = screen.getByText("2 基").parentElement?.querySelector(".badge");
  if (!badge) throw new Error("判定チップが見つかりません");
  return badge as HTMLElement;
}

/**
 * 溢れが画面に出ない面は 1 つではない。**部品の契約は `ui/ScrollArea.test.tsx` が
 * 固定するので、ここが見るのは「この面が実際にそれを使っているか」だけ。**
 * jsdom はレイアウトを持たないので、溢れているかどうかは寸法を置いて作る。
 */
function stubScroll(
  el: Element,
  size: { clientHeight: number; scrollHeight: number; scrollTop: number },
) {
  for (const [key, value] of Object.entries(size)) {
    Object.defineProperty(el, key, { value, configurable: true });
  }
  fireEvent.scroll(el);
}

describe("MotorSummary", () => {
  it("モータが 1 基でも fault なら、温度が正常でも All operational を出さない", () => {
    // サマリーが温度しきい値しか見ていなかったため、行のバッジが FAULT (赤) を
    // 出している同じ画面で、見出しだけが緑の「All operational」を出していた
    render(
      <MotorSummary
        motors={MOTORS}
        healthMotors={[
          motorHealth({ name: "y_axis_r", state: "fault" }),
          motorHealth({ name: "y_axis_l", state: "ok" }),
        ]}
      />,
    );

    expect(screen.queryByText("All operational")).not.toBeInTheDocument();
    expect(verdictBadge()).toHaveClass("badge-error");
    expect(screen.getByText("異常 1 件")).toBeInTheDocument();
  });

  it("stale は warning で件数を出す (fault ほどではないが黙らない)", () => {
    render(
      <MotorSummary
        motors={MOTORS}
        healthMotors={[
          motorHealth({ name: "y_axis_r", state: "stale" }),
          motorHealth({ name: "y_axis_l", state: "ok" }),
        ]}
      />,
    );

    expect(screen.getByText("異常 1 件")).toBeInTheDocument();
    expect(verdictBadge()).toHaveClass("badge-warning");
  });

  it("ヘルス未配信を success へ倒さない (異常の有無が分からない状態)", () => {
    render(<MotorSummary motors={MOTORS} />);

    expect(screen.queryByText("All operational")).not.toBeInTheDocument();
    expect(verdictBadge()).not.toHaveClass("badge-success");
    expect(screen.getByText("ヘルス未取得")).toBeInTheDocument();
  });

  it("全て ok なら All operational", () => {
    render(
      <MotorSummary
        motors={MOTORS}
        healthMotors={[motorHealth({ name: "y_axis_r" }), motorHealth({ name: "y_axis_l" })]}
      />,
    );

    expect(screen.getByText("All operational")).toBeInTheDocument();
    expect(verdictBadge()).toHaveClass("badge-success");
  });

  it("モータが 1 基も無ければ一覧ごと出さない", () => {
    render(<MotorSummary motors={{}} healthMotors={[]} />);

    expect(screen.getByText("モータ情報なし")).toBeInTheDocument();
  });
});

/**
 * 24 基は操縦者の右レール (285px) にも Monitor の 1 機ぶん (645px) にも収まらない。
 * 一覧が切れていることが画面に出ないと、下にあるモータが「居ない」のと区別が付かない。
 */
describe("MotorSummary の溢れ", () => {
  it("下に続きがあるあいだだけ合図を出す", () => {
    const { container } = render(<MotorSummary motors={MOTORS} />);
    const body = container.querySelector(".scroll");
    if (!body) throw new Error("スクロール面が見つからない");

    stubScroll(body, { clientHeight: 100, scrollHeight: 900, scrollTop: 0 });
    expect(container.querySelector(".bg-linear-to-t")).not.toBeNull();

    stubScroll(body, { clientHeight: 100, scrollHeight: 100, scrollTop: 0 });
    expect(container.querySelector(".bg-linear-to-t")).toBeNull();
  });
});
