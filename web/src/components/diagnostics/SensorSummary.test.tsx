import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { SensorSummary } from "@/components/diagnostics/SensorSummary";
import { MALFORMED } from "@/lib/protocol";

/** センサ名の隣に出る状態チップ */
function badgeFor(name: string): HTMLElement {
  const badge = screen.getByText(name).parentElement?.querySelector(".badge");
  if (!badge) throw new Error(`${name} の状態チップが見つかりません`);
  return badge as HTMLElement;
}

describe("SensorSummary", () => {
  it("センサ名を配信のまま出す (UI に名前を書き写さない)", () => {
    render(<SensorSummary sensors={{ brand_new_sensor: { active: false, stale: false } }} />);
    expect(screen.getByText("brand_new_sensor")).toBeInTheDocument();
  });

  /**
   * 原点合わせは「触れさせる」操作なので、接触は平常の情報であって異常ではない。
   * サーバー側もドライバの `is_fault()` に入れていない。
   */
  it("接触に警告色を使わない (接触は異常ではない)", () => {
    render(<SensorSummary sensors={{ origin_sensor: { active: true, stale: false } }} />);
    const badge = badgeFor("origin_sensor");
    expect(badge).toHaveTextContent("接触");
    expect(badge.className).not.toContain("badge-warning");
    expect(badge.className).not.toContain("badge-error");
  });

  it("接触と開放を描き分ける (指差喚呼は押した瞬間の切り替わりを見る)", () => {
    render(
      <SensorSummary
        sensors={{
          origin_sensor: { active: true, stale: false },
          rotate_origin_sensor: { active: false, stale: false },
        }}
      />,
    );
    expect(badgeFor("origin_sensor")).toHaveTextContent("接触");
    expect(badgeFor("rotate_origin_sensor")).toHaveTextContent("開放");
  });

  /**
   * 途絶したセンサを「開放」と描くと、零点確定は探索距離いっぱいまで機構を
   * 押し込んでから失敗する。異常なのは接触ではなくこちら。
   */
  it("途絶を接触状態より優先して出す", () => {
    render(<SensorSummary sensors={{ origin_sensor: { active: true, stale: true } }} />);
    const badge = badgeFor("origin_sensor");
    expect(badge).toHaveTextContent("STALE");
    expect(badge.className).toContain("badge-warning");
  });

  it("接触を報告できないドライバは — を出す (開放と混ぜない)", () => {
    render(<SensorSummary sensors={{ origin_sensor: { active: null, stale: false } }} />);
    expect(badgeFor("origin_sensor")).toHaveTextContent("—");
  });

  it("読めなかった配信は異常として主張する (黙って空へ倒さない)", () => {
    render(<SensorSummary sensors={MALFORMED} />);
    expect(screen.getByText("センサ 判定不能")).toBeInTheDocument();
  });

  it("未配信とセンサ無しの構成では 1px も占めない", () => {
    const { container: missing } = render(<SensorSummary sensors={undefined} />);
    expect(missing).toBeEmptyDOMElement();

    const { container: none } = render(<SensorSummary sensors={{}} />);
    expect(none).toBeEmptyDOMElement();
  });
});
