import { act, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AppHeader } from "@/components/shell/AppHeader";
import type { MatchState } from "@/lib/protocol";
import { MALFORMED } from "@/lib/protocol";
import type { RobotContextValue } from "@/test/robotContext";
import { DEFAULT_MATCH_STATE, renderWithRobot } from "@/test/robotContext";

/** タブ帯の描画回数を数える。ヘッダー本体が描き直されたときだけ増える */
const counts = vi.hoisted(() => ({ tabBar: 0 }));

vi.mock("@/components/shell/TabBar", () => ({
  TabBar: () => {
    counts.tabBar += 1;
    // 実体を返すのは帯の中での位置を DOM 順で見るテストがあるため。
    // null を返していた頃は「タブ帯が最左か」を確かめる手段が無かった
    return <div data-testid="tab-bar" />;
  },
}));

beforeEach(() => {
  counts.tabBar = 0;
});

function mount(over: Partial<MatchState> = {}, context: Partial<RobotContextValue> = {}) {
  return renderWithRobot(
    <MemoryRouter>
      <AppHeader />
    </MemoryRouter>,
    { matchState: { ...DEFAULT_MATCH_STATE, ...over }, ...context },
  );
}

/** `earlier` が `later` より DOM 順で前にあるか */
function precedes(earlier: Element, later: Element): boolean {
  return Boolean(earlier.compareDocumentPosition(later) & Node.DOCUMENT_POSITION_FOLLOWING);
}

/**
 * フェーズチップとコートチップは `Record` の索引で描く。未知の値が素通しで入ると
 * 索引が `undefined` になり、`StatusBadge` がクラス無し・文字無しで描かれて
 * **帯からチップごと消える**。コートは「誤設定のまま試合に入る事故を防ぐため
 * 常時表示している」要素なので、消えたことに誰も気付けない。
 */
describe("AppHeader のフェーズ・コート表示", () => {
  it("既知の値はそのまま出す", () => {
    mount({ phase: "match", court: "blue" });

    expect(screen.getByText("試合中")).toBeInTheDocument();
    expect(screen.getByText("青コート")).toBeInTheDocument();
  });

  it("読めなかったフェーズを空白にせず「不明」として出す", () => {
    mount({ phase: MALFORMED });

    expect(screen.getByText("フェーズ不明")).toBeInTheDocument();
  });

  it("読めなかったコートを空白にせず「不明」として出す", () => {
    mount({ court: MALFORMED });

    expect(screen.getByText("コート不明")).toBeInTheDocument();
  });

  it("緊急停止は何があっても押せる位置に残る", () => {
    // ヘッダーは RouteErrorBoundary の外。ここが描けなくなると止める手段が消える
    mount({ phase: MALFORMED, court: MALFORMED });

    expect(screen.getByRole("button", { name: "緊急停止" })).toBeInTheDocument();
  });
});

/**
 * **EMG STOP の周囲に押下可能な要素を置かない。** 誤爆の向きは「隣のボタンを
 * 押そうとして EMG STOP を踏む」で、試合中にこれが起きると走っているシーケンスが
 * その場で止まる。配置そのものは実機を描くまで目で確かめられないので、
 * ここでは DOM 順として固定する（`ml-6` の緩衝と合わせて 1 組の防護になっている）。
 */
describe("AppHeader の緊急停止まわりの配置", () => {
  const eStop = () => screen.getByRole("button", { name: "緊急停止" });
  const connection = () => screen.getByRole("button", { name: /Connected/ });

  it("押下可能な要素は EMG STOP より手前に置く", () => {
    // 停止ボタンより後ろに押せるものが 1 つでもあれば、そこが新しい誤爆源になる。
    // 「隣を狙って外した先が EMG STOP」という配置を禁じるのがこのテストの役目
    const { container } = mount();
    const buttons = Array.from(container.querySelectorAll("button"));

    // 押せるものが停止ボタンしか無いなら「最後である」は自明で、何も見ていない
    expect(buttons.length).toBeGreaterThan(1);
    expect(buttons.at(-1)).toBe(eStop());
  });

  it("EMG STOP の直左には押せない要素を置く", () => {
    // 接続表示 (押せる) を右群の先頭へ、フェーズ・コートのチップ (読むだけ) を
    // 停止ボタン側へ寄せる。逆順にすると、接続表示を狙った 1 回が停止になる
    mount();

    expect(precedes(connection(), screen.getByText("セッティングタイム"))).toBe(true);
    expect(precedes(connection(), screen.getByText("赤コート"))).toBe(true);
  });

  it("タブ帯はヘッダーの最左に置く", () => {
    // 画面の隅はポインタで最も当てやすく、同時に EMG STOP から最も遠い。
    // タブ帯を右へ戻すと、最も頻繁に押す要素が停止ボタンの隣に来る
    mount();
    const tabBar = screen.getByTestId("tab-bar");

    expect(precedes(tabBar, connection())).toBe(true);
    expect(precedes(tabBar, screen.getByText("セッティングタイム"))).toBe(true);
    expect(precedes(tabBar, eStop())).toBe(true);
  });

  it("ヘッダーの EMG STOP は点滅させない", () => {
    // 平常時から点滅している唯一の要素だった。常時動くものが 1 つあると、
    // 本当に異常が出たときの点滅まで平常の一部として読み飛ばされる。
    // 異常時にだけ描かれる EStopOverlay / ConnectionBanner の点滅は別物で、残す
    const { container } = mount();

    expect(eStop()).toBeInTheDocument();
    expect(container.querySelector(".alert-blink")).toBeNull();
  });
});

describe("AppHeader の接続表示", () => {
  it("接続表示そのものが接続先設定の入口になっている", async () => {
    // 繋がらないときに操縦者が最初に見る場所。ここが入口でないと、
    // 接続先を変える手段が平常時の画面から消える
    const { context } = mount();

    await userEvent.click(screen.getByRole("button", { name: /Connected/ }));

    expect(context.openWsSettings).toHaveBeenCalled();
  });

  it("切断中は接続先とともに切断を出す", () => {
    mount({}, { connected: false, wsUrl: "ws://drc:8080/ws" });

    expect(screen.getByRole("button", { name: /Disconnected/ })).toBeInTheDocument();
    expect(screen.getByText("drc:8080")).toBeInTheDocument();
  });
});

/**
 * 時計は毎秒 `setState` する。ヘッダー本体へインライン展開すると、タブ帯を含む
 * ヘッダー全体が 1 秒ごとに描き直される（試合中ずっと続く）。
 * 独立したコンポーネントに保つことでその更新を時計の中へ閉じ込める。
 */
describe("ヘッダーの時計", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("秒が進んでもヘッダー本体を描き直さない", () => {
    vi.setSystemTime(new Date(2026, 0, 1, 19, 5, 50));
    mount();
    const before = counts.tabBar;
    const shown = screen.getByText(/^\d{1,2}:\d{2}:\d{2}$/).textContent;

    act(() => vi.advanceTimersByTime(3000));

    // 時計自身は進んでいる（進んでいなければ「描き直さない」は自明で無意味）
    expect(screen.getByText(/^\d{1,2}:\d{2}:\d{2}$/).textContent).not.toBe(shown);
    expect(counts.tabBar).toBe(before);
  });
});
