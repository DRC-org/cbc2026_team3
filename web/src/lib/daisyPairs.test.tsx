import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { StatusBadge } from "@/components/ui/StatusBadge";
import { TABS } from "@/lib/tabs";
import type { Tone } from "@/lib/tone";
import {
  TONE_BADGE_CLASS,
  TONE_BORDER_CLASS,
  TONE_PROGRESS_CLASS,
  TONE_STATUS_CLASS,
  TONE_TEXT_CLASS,
} from "@/lib/tone";

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

  it("全トーンに文字色・進捗色・枠色が定義されている", () => {
    for (const tone of ALL_TONES) {
      expect(TONE_TEXT_CLASS[tone]).toBeTruthy();
      expect(TONE_BORDER_CLASS[tone]).toBeTruthy();
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
});
