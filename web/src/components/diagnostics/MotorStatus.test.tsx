import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { MotorStatus } from "@/components/diagnostics/MotorStatus";
import type { MotorState } from "@/lib/protocol";
import { motorState } from "@/test/motorState";

/**
 * 診断カラムの数値 4 列。
 *
 * **測る手段が無い項目に 0 を描いてはならない。** 自作モータドライバの DC 基板
 * (コンベア) と電磁弁基板はエンコーダも電流センスも温度センサも積んでおらず、
 * CAN プロトコルにフィールド自体が無い。`0.0 / 0.0 / 0.0 / 0.0℃` と出ていた頃は、
 * 操縦者に「本当に 0 なのか、フィードバックが来ていないのか」を区別する手段が
 * 画面のどこにも無かった。
 */
const THRESHOLDS = { warning: 60, critical: 80 };

/** 値行 (POS/VEL/TRQ/TMP) のセル。見出しは別部品なのでここには出ない */
function cells(): HTMLElement[] {
  const row = document.querySelector(".grid-cols-4");
  if (!row) throw new Error("数値行が見つかりません");
  return Array.from(row.children) as HTMLElement[];
}

describe("MotorStatus", () => {
  it("測れない 4 値を「—」で描き、単位も付けない (DC 基板・電磁弁基板)", () => {
    render(
      <MotorStatus
        name="conveyor"
        state={motorState({ pos: null, vel: null, torque: null, temp: null })}
        tempThresholds={THRESHOLDS}
      />,
    );

    expect(cells().map((c) => c.textContent)).toEqual(["—", "—", "—", "—"]);
    // `—℃` は意味を持たない。単位は値があるときだけ
    expect(screen.queryByText("℃")).not.toBeInTheDocument();
    // 「測ったように見える 0」が 1 つでも残っていないこと
    expect(screen.queryByText("0.0")).not.toBeInTheDocument();
    // 温度が測れないモータには色も付けない。0 を温度とみなすとしきい値以下なので
    // 「正常」の側へ黙って倒れる (判定そのものは motorTempTone が持ち、
    // `healthVerdict.test.ts` が null → neutral を固定している)
    const [, , , tmp] = cells();
    expect(tmp).not.toHaveClass("text-warning");
    expect(tmp).not.toHaveClass("text-error");
  });

  it("測れる項目は従来どおり数値と単位を出す (M3508 等)", () => {
    render(
      <MotorStatus
        name="y_axis_r"
        state={motorState({ pos: 12.34, vel: -5, torque: 0.5, temp: 41.2 })}
        tempThresholds={THRESHOLDS}
      />,
    );

    expect(cells().map((c) => c.textContent)).toEqual(["12.3", "-5.0", "0.5", "41.2℃"]);
  });

  it("測れる項目と測れない項目が混ざっても、測れた側は数値のまま (サーボ基板は位置だけ)", () => {
    render(
      <MotorStatus
        name="wall"
        state={motorState({ pos: 90, vel: null, torque: null, temp: null })}
        tempThresholds={THRESHOLDS}
      />,
    );

    expect(cells().map((c) => c.textContent)).toEqual(["90.0", "—", "—", "—"]);
  });

  it("高温は従来どおり着色する (色分けが「—」対応で消えていないこと)", () => {
    render(
      <MotorStatus name="y_axis_r" state={motorState({ temp: 85 })} tempThresholds={THRESHOLDS} />,
    );

    expect(cells()[3]).toHaveClass("text-error");
  });

  it("桁位置を揃えるグリッドと等幅指定を「—」でも崩さない", () => {
    render(<MotorStatus name="conveyor" state={motorState({ pos: null })} />);

    // 4 列グリッドの中に 4 セルが並び、どれも tabular-nums のままであること
    // (右寄せはグリッド側の text-right が持つ)
    const all = cells();
    expect(all).toHaveLength(4);
    for (const cell of all) {
      expect(cell).toHaveClass("font-mono");
      expect(cell).toHaveClass("tabular-nums");
    }
  });

  it("欄そのものが欠けた配信は「—」ではなく異常側へ倒す", () => {
    // **測れない (null) と読めない (欠落・型違い) を混ぜてはならない。**
    // 同じ「—」にすると、配信の不具合が「このドライバは測れない」として
    // 画面から消える。ついでに `state.pos.toFixed()` がレンダー本体で投げて
    // React ツリーごとアンマウントする経路も塞ぐ
    const broken = { ...motorState(), pos: undefined, vel: "x" } as unknown as MotorState;
    render(<MotorStatus name="y_axis_r" state={broken} />);

    const [pos, vel] = cells();
    expect(pos).toHaveTextContent("?");
    expect(pos).toHaveClass("text-error");
    expect(vel).toHaveTextContent("?");
  });
});
