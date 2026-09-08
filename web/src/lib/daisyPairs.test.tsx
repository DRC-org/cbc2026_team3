import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";

import { TabBar } from "@/components/shell/TabBar";
import { Toaster } from "@/components/shell/Toaster";
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
    for (const tone of ALL_TONES.filter((t) => t !== "neutral")) {
      expect(TONE_STATUS_CLASS[tone]).toMatch(new RegExp(`(^| )status-${tone}( |$)`));
    }
  });

  it("alert は親クラスと色修飾子が揃っている", () => {
    for (const tone of ALL_TONES) {
      expect(TONE_ALERT_CLASS[tone]).toMatch(/(^| )alert( |$)/);
    }
    for (const tone of ALL_TONES.filter((t) => t !== "neutral")) {
      expect(TONE_ALERT_CLASS[tone]).toMatch(new RegExp(`(^| )alert-${tone}( |$)`));
    }
  });

  it("トーストの配置は親クラスと位置修飾子が揃っている", () => {
    const { container } = renderWithRobot(<Toaster />, {
      rejection: {
        command: "match_start",
        reason: "チェックリスト未完了",
        receivedAtMs: 1,
        source: "server",
      },
    });

    expect(container.firstElementChild).toHaveClass("toast", "toast-end", "toast-bottom");
  });

  it("Panel のアクセントバーは太さと色をリテラルで揃えて出す", () => {
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
    for (const tone of ALL_TONES) {
      expect(TONE_TEXT_CLASS[tone]).toBeTruthy();
      expect(TONE_BORDER_L_CLASS[tone]).toMatch(/(^| )border-l-[a-z0-9-]+( |$)/);
      if (tone !== "neutral") expect(TONE_PROGRESS_CLASS[tone]).toBeTruthy();
    }
  });

  it("StatusBadge は badge と status の両方を描画する", () => {
    render(<StatusBadge tone="warning">許可待ち</StatusBadge>);
    const badge = screen.getByText("許可待ち").parentElement;
    expect(badge).toHaveClass("badge", "badge-soft", "badge-warning");
    expect(badge?.querySelector(".status.status-warning")).not.toBeNull();
  });

  it("どのタブにも数字キーが重複せず割り当てられている", () => {
    const hotkeys = TABS.map((tab) => tab.hotkey);
    expect(new Set(hotkeys).size).toBe(TABS.length);
  });

  it("キー凡例はタブ定義から描く", () => {
    renderWithRobot(
      <MemoryRouter>
        <TabBar />
      </MemoryRouter>,
    );
    for (const tab of TABS) {
      expect(screen.getByText(tab.hotkey)).toBeInTheDocument();
    }
  });
});
