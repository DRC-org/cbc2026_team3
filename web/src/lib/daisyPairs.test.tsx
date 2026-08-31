import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { StatusBar } from "@/components/shell/StatusBar";
import { Panel } from "@/components/ui/Panel";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { TABS } from "@/lib/tabs";
import type { Tone } from "@/lib/tone";
import {
  TONE_ALERT_CLASS,
  TONE_BADGE_CLASS,
  TONE_BORDER_L_CLASS,
  TONE_PROGRESS_CLASS,
  TONE_STATUS_CLASS,
  TONE_TEXT_CLASS,
} from "@/lib/tone";
import { renderWithRobot } from "@/test/robotContext";

const ALL_TONES: Tone[] = ["success", "warning", "error", "info", "neutral"];

/**
 * daisyUI のコンポーネントは「親クラス + 修飾子」が揃って初めて成立する。
 * Tailwind はソース中に現れた文字列ぶんしか CSS を出力しないため、
 * 片方を書き忘れると **DOM には存在するのに何も見えない** 要素が出荷される
 * （過去に `modal-box` だけ書いて `modal modal-open` を落とし、実際にそうなった）。
 *
 * 文字列連結で組み立てると Tailwind の走査から漏れるので、対は必ず
 * 揃った 1 本の文字列としてソースに書く。ここではその不変条件を固定する。
 */
describe("daisyUI のクラスは対で書かれている", () => {
  it("badge は親クラスと修飾子が揃っている", () => {
    for (const tone of ALL_TONES) {
      expect(TONE_BADGE_CLASS[tone]).toMatch(/(^| )badge( |$)/);
      expect(TONE_BADGE_CLASS[tone]).toMatch(/(^| )badge-soft( |$)/);
      expect(TONE_BADGE_CLASS[tone]).toMatch(/(^| )badge-[a-z]+( |$)/);
    }
  });

  it("status は親クラスと色修飾子が揃っている", () => {
    for (const tone of ALL_TONES) {
      expect(TONE_STATUS_CLASS[tone]).toMatch(/(^| )status( |$)/);
    }
    // neutral は色修飾子を持たない（daisyUI 既定の灰色 LED をそのまま使う）
    for (const tone of ALL_TONES.filter((t) => t !== "neutral")) {
      expect(TONE_STATUS_CLASS[tone]).toMatch(new RegExp(`(^| )status-${tone}( |$)`));
    }
  });

  it("alert は親クラスと色修飾子が揃っている", () => {
    // トーストはここを使う。かつて配色表が `components/shell/Toaster.tsx` の
    // ローカル定義だったため、この検査の対象から外れていた
    for (const tone of ALL_TONES) {
      expect(TONE_ALERT_CLASS[tone]).toMatch(/(^| )alert( |$)/);
    }
    // neutral だけは色修飾子を持たない (daisyUI に alert-neutral は無い)
    for (const tone of ALL_TONES.filter((t) => t !== "neutral")) {
      expect(TONE_ALERT_CLASS[tone]).toMatch(new RegExp(`(^| )alert-${tone}( |$)`));
    }
  });

  it("Panel のアクセントバーは太さと色をリテラルで揃えて出す", () => {
    // `border-l-[0.4rem]` だけ、あるいは色だけを出すと、DOM には在るのに
    // 見えないバーになる (Tailwind はソースに現れた文字列ぶんしか CSS を出さない)
    const { container } = render(
      <Panel accentTone="warning" legend="機体状態">
        本文
      </Panel>,
    );
    const section = container.querySelector("section");
    expect(section).toHaveClass("border-l-[0.4rem]", "border-l-warning");
  });

  it("Panel は accentTone を渡さなければアクセントバーを出さない", () => {
    const { container } = render(<Panel legend="機体状態">本文</Panel>);
    expect(container.querySelector("section")?.className).not.toMatch(/border-l-/);
  });

  it("全トーンに文字色・進捗色・アクセントバー色が定義されている", () => {
    // 検査対象は実際に使われているマップだけにする。使われていないマップを
    // 「全トーン揃っている」と守り続けると、消せない死蔵コードになる
    for (const tone of ALL_TONES) {
      expect(TONE_TEXT_CLASS[tone]).toBeTruthy();
      // 実行時に border- から組み立てるのは禁止。リテラルで揃っていることを見る
      expect(TONE_BORDER_L_CLASS[tone]).toMatch(/(^| )border-l-[a-z0-9-]+( |$)/);
      // neutral だけは既定の進捗色（無指定）を使う
      if (tone !== "neutral") expect(TONE_PROGRESS_CLASS[tone]).toBeTruthy();
    }
  });

  it("StatusBadge は badge と status の両方を描画する", () => {
    render(<StatusBadge tone="warning">許可待ち</StatusBadge>);
    const badge = screen.getByText("許可待ち").parentElement;
    expect(badge).toHaveClass("badge", "badge-soft", "badge-warning");
    expect(badge?.querySelector(".status.status-warning")).not.toBeNull();
  });

  it("タブは 4 つとも数字キーが重複せず割り当てられている", () => {
    const hotkeys = TABS.map((tab) => tab.hotkey);
    expect(new Set(hotkeys).size).toBe(TABS.length);
  });

  it("ステータスバーのキー凡例はタブ定義から描く", () => {
    // 直書きしていた頃は、タブが増減してもここだけ古い数字が残った
    renderWithRobot(<StatusBar />);
    for (const tab of TABS) {
      expect(screen.getByText(tab.hotkey)).toBeInTheDocument();
    }
  });
});
