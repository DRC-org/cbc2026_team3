import { fireEvent, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { MatchPrep } from "@/components/monitor/MatchPrep";
import type { ChecklistItem, ChecklistState, MatchPhase } from "@/lib/protocol";
import { MALFORMED } from "@/lib/protocol";
import { DEFAULT_MATCH_STATE, DEFAULT_SERVER_INFO, renderWithRobot } from "@/test/robotContext";

function item(id: string, group?: string | null, checked = false): ChecklistItem {
  return { id, label: id, checked, group };
}

const ITEMS: ChecklistState = {
  items: [
    item("非常停止解除", "preflight"),
    item("コート一致", "court"),
    item("動作 OK", "motor_check"),
    item("周囲確認", "final"),
  ],
  completed: false,
};

/** `null` = ロールごと未配信 (config に項目が無い状態) */
function mount(
  checklist: ChecklistState | typeof MALFORMED | null = ITEMS,
  phase: MatchPhase = "setup",
  overrides: { devTools?: boolean; connected?: boolean } = {},
) {
  return renderWithRobot(<MatchPrep onRequestReset={vi.fn()} />, {
    connected: overrides.connected ?? true,
    matchState: {
      ...DEFAULT_MATCH_STATE,
      phase,
      court: "red",
      checklists: checklist === MALFORMED ? MALFORMED : checklist ? { pre_match: checklist } : {},
    },
    serverInfo: { ...DEFAULT_SERVER_INFO, dev_tools: overrides.devTools ?? false },
  });
}

/** 見出しから、その区分の枠 (Section) を取る */
function section(title: string): HTMLElement {
  const heading = screen.getByText(title);
  const element = heading.closest("section");
  if (!element) throw new Error(`区分が見つからない: ${title}`);
  return element;
}

/**
 * 準備の面は「操作とその確認を同じ場所に置く」ことが唯一の存在理由。
 * 項目が対応するコントロールから離れたら、この面は元の長いリストへ戻っている。
 */
describe("MatchPrep の項目配置", () => {
  it("コート選択の項目をコート選択ボタンと同じ区分に置く", () => {
    mount();

    const court = section("コート設定");
    expect(within(court).getByRole("button", { name: "赤コート" })).toBeInTheDocument();
    expect(within(court).getByLabelText("コート一致")).toBeInTheDocument();
  });

  it("動作確認の項目を動作確認ボタンと同じ区分に置く", () => {
    mount();

    const check = section("アクチュエータ動作確認");
    expect(within(check).getByRole("button", { name: "動作確認を開始" })).toBeInTheDocument();
    expect(within(check).getByLabelText("動作 OK")).toBeInTheDocument();
    // 別区分の項目を巻き込んでいない (巻き込むと元の 1 本リストに戻る)
    expect(within(check).queryByLabelText("コート一致")).toBeNull();
  });

  it("動作確認の進捗と結果もその場で開く (モーダルへ追い出さない)", () => {
    // かつてこれはモーダルで、駆動しているあいだヘッダーの EMG STOP を覆っていた
    mount();

    const check = section("アクチュエータ動作確認");
    expect(within(check).getByRole("button", { name: "手順と結果" })).toBeInTheDocument();
  });

  it("group を持たない項目・未知の group の項目も必ず操作できる形で描く", () => {
    // 落とすと、指差喚呼が 1 つ足りないまま試合開始のゲートだけが開かない。
    // ベンチ設定 (config/bench/*) は group を 1 つも持たない
    mount({
      items: [item("ベンチ項目"), item("誤記 group", "moter_check")],
      completed: false,
    });

    const other = section("その他の確認");
    expect(within(other).getByLabelText("ベンチ項目")).toBeEnabled();
    expect(within(other).getByLabelText("誤記 group")).toBeEnabled();
  });

  it("「次」の強調は画面全体で 1 つだけ (区分ごとに出すと強調でなくなる)", () => {
    mount();

    expect(screen.getAllByText("次")).toHaveLength(1);
  });

  it("全体の進捗は 1 箇所だけに出す (試合開始のゲートは全項目の完了)", () => {
    mount({
      items: [item("a", "preflight", true), item("b", "court"), item("c", "motor_check")],
      completed: false,
    });

    expect(screen.getByText("/3")).toBeInTheDocument();
    expect(screen.getByText("残り 2")).toBeInTheDocument();
  });
});

describe("MatchPrep のチェック操作", () => {
  it("チェック操作をサーバーへ送る (状態はサーバー保持のため)", async () => {
    const { context } = mount();

    await userEvent.click(screen.getByLabelText("コート一致"));

    expect(context.setChecklistItem).toHaveBeenCalledWith("pre_match", "コート一致", true);
  });

  it("試合中はチェックを触らせない (サーバーも PHASES_PREPARATION で拒否する)", () => {
    mount(ITEMS, "match");

    expect(screen.getByLabelText("コート一致")).toBeDisabled();
  });

  it("何もチェックしていなければ RESET を押させない (無意味な確認を出さない)", () => {
    mount({ items: [item("a", "preflight")], completed: false });

    expect(screen.getByRole("button", { name: /リセット/ })).toBeDisabled();
  });

  it("DEV 全チェックは --dev-tools 起動でしか出さない", () => {
    mount();
    expect(screen.queryByRole("button", { name: /開発用に全てチェック/ })).toBeNull();

    mount(ITEMS, "setup", { devTools: true });
    expect(screen.getByRole("button", { name: /開発用に全てチェック/ })).toBeEnabled();
  });
});

/**
 * コート選択は「今どちらか」を色で示す唯一の場所 (誤ったコートのまま試合に入る事故は
 * 試合をそのまま落とす)。選択中の面はコートの色そのものでなければ意味を成さないため、
 * 汎用の反転表示 (`Button` の generic な selected) ではなく呼び出し側が色を持つ。
 */
describe("MatchPrep のコート選択", () => {
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
    mount(ITEMS, "match");

    expect(screen.getByRole("button", { name: "赤コート" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "青コート" })).toBeDisabled();
  });

  it("準備中のリセットは確認を挟む（まだ使っていない指差喚呼を捨てるため）", () => {
    // MatchStrip の「セッティングへ戻る」は即実行だが、こちらは同じ match_reset でも
    // 失うものが違う。押した時点で完了済みの指差喚呼が全て消える
    const onRequestReset = vi.fn();
    const { context } = renderWithRobot(<MatchPrep onRequestReset={onRequestReset} />, {
      matchState: {
        ...DEFAULT_MATCH_STATE,
        phase: "setup",
        court: "red",
        checklists: { pre_match: { items: [item("a", "preflight", true)], completed: false } },
      },
    });

    fireEvent.click(screen.getByRole("button", { name: /リセット/ }));

    expect(onRequestReset).toHaveBeenCalledTimes(1);
    expect(context.matchReset).not.toHaveBeenCalled();
  });

  it("やり直しの導線は 1 つだけ (結果が同じボタンを 2 つ並べない)", () => {
    // かつてヘッダの CLEAR (checklist_reset) と最下段の match_reset が並んでいたが、
    // 準備フェーズではフェーズもタイマーも初期状態なので結果が同じで、
    // 操縦者はどちらを押すべきか画面から判断できなかった
    mount({ items: [item("a", "preflight", true)], completed: false });

    expect(screen.getAllByRole("button", { name: /リセット|解除/ })).toHaveLength(1);
  });

  it("配信を読めていない間もリセットは押せる (直す手段まで消さない)", () => {
    mount(MALFORMED);

    expect(screen.getByRole("button", { name: /リセット/ })).toBeEnabled();
  });
});

describe("MatchPrep の配信異常", () => {
  it("読めない配信を「項目が未定義」へ倒さない", () => {
    // 空は config に項目が無いことの表現として既に使っている。混ぜると
    // 操縦者は config/checklist.yaml を疑って探しに行く
    mount(MALFORMED);

    expect(screen.getByText(/配信を読めていません/)).toBeInTheDocument();
    expect(screen.queryByText(/config\/checklist\.yaml/)).toBeNull();
  });

  it("項目が未定義なら設定ファイルの場所を案内する", () => {
    mount(null);
    expect(screen.getByText(/config\/checklist\.yaml/)).toBeInTheDocument();
  });

  it("チェックリストが読めなくてもコート設定と動作確認は操作できる", () => {
    // 直す手段まで画面から消すと、配信が壊れた時点で準備そのものが進まなくなる
    mount(MALFORMED);

    expect(screen.getByRole("button", { name: "青コート" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "動作確認を開始" })).toBeInTheDocument();
  });
});

/**
 * チェック状態はサーバー配信が唯一の出どころ。切断中に押せるままにすると
 * **チェックが付かないだけで理由も出ない** —— 試合前の最も忙しい時間帯に、
 * 最も紛らわしい挙動になる。同じ画面のコート選択は最初から `connected` を見ており、
 * `StartGate` も「通信 — サーバーに接続できていません」を出している。
 */
describe("MatchPrep の切断中", () => {
  it("指差喚呼のチェックボックスを押せなくする", () => {
    mount(ITEMS, "setup", { connected: false });

    expect(screen.getByLabelText("非常停止解除")).toBeDisabled();
    expect(screen.getByLabelText("コート一致")).toBeDisabled();
  });

  it("コート選択と同じ扱いにする (片方だけ生きている状態を作らない)", () => {
    mount(ITEMS, "setup", { connected: false });

    expect(screen.getByRole("button", { name: "赤コート" })).toBeDisabled();
  });

  it("接続中は今までどおり押せる", () => {
    mount(ITEMS, "setup", { connected: true });

    expect(screen.getByLabelText("非常停止解除")).toBeEnabled();
  });
});
