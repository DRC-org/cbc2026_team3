import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { MotorStatHeader, MotorStatus } from "@/components/diagnostics/MotorStatus";
import type { MotorState } from "@/lib/protocol";
import { motorState } from "@/test/motorState";

const THRESHOLDS = { warning: 60, critical: 80 };

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
    expect(screen.queryByText("℃")).not.toBeInTheDocument();
    expect(screen.queryByText("0.0")).not.toBeInTheDocument();
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

    const all = cells();
    expect(all).toHaveLength(4);
    for (const cell of all) {
      expect(cell).toHaveClass("font-mono");
      expect(cell).toHaveClass("tabular-nums");
    }
  });

  it("欄そのものが欠けた配信は「—」ではなく異常側へ倒す", () => {
    const broken = { ...motorState(), pos: undefined, vel: "x" } as unknown as MotorState;
    render(<MotorStatus name="y_axis_r" state={broken} />);

    const [pos, vel] = cells();
    expect(pos).toHaveTextContent("?");
    expect(pos).toHaveClass("text-error");
    expect(vel).toHaveTextContent("?");
  });

  describe("指令値の表示 (フィードバックを持たない基板)", () => {
    it("DC 基板の duty 指令を POS 欄へ「→」付きで出す (小数 2 桁)", () => {
      render(
        <MotorStatus
          name="conveyor"
          state={motorState({
            pos: null,
            vel: null,
            torque: null,
            temp: null,
            command: 0.3,
            command_mode: "duty",
          })}
        />,
      );

      expect(cells().map((c) => c.textContent)).toEqual(["→0.30", "—", "—", "—"]);
    });

    it("電磁弁の on_off 指令は数値ではなく ON / OFF", () => {
      const valve = (command: number) =>
        motorState({
          pos: null,
          vel: null,
          torque: null,
          temp: null,
          command,
          command_mode: "on_off",
        });

      const { unmount } = render(<MotorStatus name="valve_1" state={valve(1)} />);
      expect(cells()[0]).toHaveTextContent("→ON");
      unmount();

      render(<MotorStatus name="valve_1" state={valve(0)} />);
      expect(cells()[0]).toHaveTextContent("→OFF");
    });

    it("実測値があるモータには指令値を出さない (同じ欄を実測と指令で読み分けさせない)", () => {
      render(
        <MotorStatus
          name="y_axis_r"
          state={motorState({ pos: 1500, command: 1500, command_mode: "position" })}
        />,
      );

      expect(cells()[0]).toHaveTextContent("1500.0");
      expect(cells()[0].textContent).not.toContain("→");
    });

    it("一度も指令していないモータは「—」のまま (0 とも ON とも言わない)", () => {
      render(
        <MotorStatus
          name="conveyor"
          state={motorState({ pos: null, vel: null, torque: null, temp: null, command: null })}
        />,
      );

      expect(cells().map((c) => c.textContent)).toEqual(["—", "—", "—", "—"]);
    });

    it("指令値であって実出力ではないことを言葉でも出す", () => {
      render(
        <MotorStatus
          name="conveyor"
          state={motorState({ pos: null, command: 0.3, command_mode: "duty" })}
        />,
      );

      expect(cells()[0]).toHaveAttribute(
        "title",
        expect.stringContaining("実際の出力ではありません"),
      );
    });

    it("指令の欄そのものが未配信なら「—」。異常側へ倒さない", () => {
      const old = { ...motorState({ pos: null }), command: undefined } as unknown as MotorState;
      render(<MotorStatus name="conveyor" state={old} />);

      expect(cells()[0]).toHaveTextContent("—");
    });

    it("指令の欄が型違いなら異常側 (未配信と混ぜない)", () => {
      const broken = { ...motorState({ pos: null }), command: "0.3" } as unknown as MotorState;
      render(<MotorStatus name="conveyor" state={broken} />);

      expect(cells()[0]).toHaveTextContent("?");
      expect(cells()[0]).toHaveClass("text-error");
    });
  });
});

function spacer(): HTMLElement {
  const { container } = render(<MotorStatHeader />);
  const el = container.querySelector("[aria-hidden]");
  if (!el) throw new Error("見出しの空きが見つかりません");
  return el as HTMLElement;
}

function widthClasses(el: HTMLElement): string[] {
  return [...el.classList].filter((c) => c.includes(":w-["));
}

describe("MotorStatHeader", () => {
  it("`hidden` を打ち消す display を、幅と同じブレークポイントで持つ", () => {
    const el = spacer();
    const [width] = widthClasses(el);
    expect(width).toBeDefined();

    expect(el.className).toContain(`${width.split(":")[0]}:block`);
  });

  it("空きの幅はモータ行の名前列と同じクラスで、ずれないこと", () => {
    const { container } = render(<MotorStatus name="y_axis_r" state={motorState({})} />);
    const nameColumn = container.firstElementChild?.firstElementChild as HTMLElement;

    expect(widthClasses(spacer())).toEqual(widthClasses(nameColumn));
    expect(widthClasses(nameColumn)).toHaveLength(1);
  });
});
