import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ManualAxisRow } from "@/components/operator/ManualAxisRow";
import type { ManualAxis } from "@/lib/protocol";

const PAIRED: ManualAxis = {
  name: "y_axis",
  unit: "mm",
  command_mode: "position",
  value: 5.0,
  target: 4.0,
  manual: { min: -2, max: 20, steps: [0.5, 2] },
  manual_always: false,
  deviation: 0.2,
  sync_tolerance: 2.0,
  positions: [
    { name: "home", value: 0 },
    { name: "work", value: 15 },
  ],
  motors: ["y_axis_r", "y_axis_l"],
};

const DISCRETE: ManualAxis = {
  name: "gripper",
  unit: "deg",
  command_mode: "position",
  value: 5.0,
  target: null,
  manual: null,
  manual_always: false,
  deviation: null,
  sync_tolerance: null,
  positions: [
    { name: "open", value: 5 },
    { name: "closed", value: 0 },
  ],
  motors: ["gripper"],
};

const DUTY: ManualAxis = {
  name: "conveyor",
  unit: "duty",
  command_mode: "duty",
  value: null,
  target: null,
  manual: null,
  manual_always: true,
  deviation: null,
  sync_tolerance: null,
  positions: [
    { name: "stop", value: 0 },
    { name: "run", value: 0.3 },
  ],
  motors: ["conveyor"],
};

const VALVE: ManualAxis = {
  name: "valve_1",
  unit: "on_off",
  command_mode: "on_off",
  value: null,
  target: null,
  manual: null,
  manual_always: true,
  deviation: null,
  sync_tolerance: null,
  positions: [
    { name: "open", value: 1 },
    { name: "closed", value: 0 },
  ],
  motors: ["valve_1"],
};

function renderRow(axis: ManualAxis, blockedReason: string | null = null, selected = false) {
  const onJog = vi.fn();
  const onSet = vi.fn();
  const onMove = vi.fn();
  const onSelect = vi.fn();
  const view = render(
    <ManualAxisRow
      axis={axis}
      blockedReason={blockedReason}
      selected={selected}
      onSelect={onSelect}
      onJog={onJog}
      onSet={onSet}
      onMove={onMove}
    />,
  );
  return { onJog, onSet, onMove, onSelect, view };
}

describe("ManualAxisRow", () => {
  describe("連続操作できる軸", () => {
    it("ジョグ・絶対値・プリセットの 3 経路が出る", () => {
      renderRow(PAIRED);
      expect(screen.getByLabelText("y_axis を 0.5mm 進める")).toBeInTheDocument();
      expect(screen.getByLabelText("y_axis の目標値")).toBeInTheDocument();
      expect(screen.getByLabelText("y_axis を home へ")).toBeInTheDocument();
    });

    it("ジョグは選択中のステップ量を符号付きで送る", async () => {
      const user = userEvent.setup();
      const { onJog } = renderRow(PAIRED);

      await user.click(screen.getByLabelText("y_axis を 0.5mm 進める"));
      await user.click(screen.getByLabelText("y_axis を 0.5mm 戻す"));

      expect(onJog.mock.calls).toEqual([
        ["y_axis", 0.5],
        ["y_axis", -0.5],
      ]);
    });

    it("ステップ量を変えるとジョグ量も変わる", async () => {
      const user = userEvent.setup();
      const { onJog } = renderRow(PAIRED);

      await user.selectOptions(
        screen.getByLabelText("y_axis のジョグ量"),
        screen.getByRole("option", { name: "2 mm" }),
      );
      await user.click(screen.getByLabelText("y_axis を 2mm 進める"));

      expect(onJog).toHaveBeenCalledWith("y_axis", 2);
    });

    it("浮動小数の候補でも選んだ量がそのまま飛ぶ", async () => {
      const user = userEvent.setup();
      const { onJog } = renderRow({
        ...PAIRED,
        manual: { min: -2, max: 20, steps: [0.5, 0.05, 1.25] },
      });

      await user.selectOptions(
        screen.getByLabelText("y_axis のジョグ量"),
        screen.getByRole("option", { name: "0.05 mm" }),
      );
      await user.click(screen.getByLabelText("y_axis を 0.05mm 進める"));

      expect(onJog).toHaveBeenCalledWith("y_axis", 0.05);
    });

    it("刻みの候補が 1 つも無ければ連続操作を出さない (1 を捏造しない)", () => {
      renderRow({ ...PAIRED, manual: { min: -2, max: 20, steps: [] } });

      expect(screen.queryByLabelText("y_axis のジョグ量")).toBeNull();
      expect(screen.queryByLabelText(/y_axis を .* 進める/)).toBeNull();
    });

    it("絶対値は入力して送信したときだけ飛ぶ", async () => {
      const user = userEvent.setup();
      const { onSet } = renderRow(PAIRED);
      const input = screen.getByLabelText("y_axis の目標値");

      await user.clear(input);
      await user.type(input, "12.5");
      expect(onSet).not.toHaveBeenCalled();

      await user.click(screen.getByLabelText("y_axis を入力値へ移動"));
      expect(onSet).toHaveBeenCalledWith("y_axis", 12.5);
    });

    it("空欄では送信できない", async () => {
      const user = userEvent.setup();
      renderRow(PAIRED);
      await user.clear(screen.getByLabelText("y_axis の目標値"));
      expect(screen.getByLabelText("y_axis を入力値へ移動")).toBeDisabled();
    });

    it("可動範囲はドラッグできる入力にしない", () => {
      renderRow(PAIRED);
      expect(screen.queryByRole("slider")).toBeNull();
    });

    it("上限に達したら進める側を塞ぐ", () => {
      renderRow({ ...PAIRED, target: 20 });
      expect(screen.getByLabelText("y_axis を 0.5mm 進める")).toBeDisabled();
      expect(screen.getByLabelText("y_axis を 0.5mm 戻す")).toBeEnabled();
    });

    it("下限に達したら戻す側を塞ぐ", () => {
      renderRow({ ...PAIRED, target: -2 });
      expect(screen.getByLabelText("y_axis を 0.5mm 戻す")).toBeDisabled();
      expect(screen.getByLabelText("y_axis を 0.5mm 進める")).toBeEnabled();
    });

    it("端の判定は現在値ではなく直前の手動目標を基準にする", () => {
      renderRow({ ...PAIRED, value: 0, target: 20 });
      expect(screen.getByLabelText("y_axis を 0.5mm 進める")).toBeDisabled();
    });
  });

  describe("大きく動かす", () => {
    it("可動範囲の端へ 1 回で飛べる", async () => {
      const user = userEvent.setup();
      const { onSet } = renderRow(PAIRED);

      await user.click(screen.getByLabelText("y_axis を上限 20mm へ"));
      await user.click(screen.getByLabelText("y_axis を下限 -2mm へ"));

      expect(onSet.mock.calls).toEqual([
        ["y_axis", 20],
        ["y_axis", -2],
      ]);
    });

    it("端に居るならその端へのボタンは塞ぐ", () => {
      renderRow({ ...PAIRED, target: 20 });
      expect(screen.getByLabelText("y_axis を上限 20mm へ")).toBeDisabled();
      expect(screen.getByLabelText("y_axis を下限 -2mm へ")).toBeEnabled();
    });

    it("入力欄には直前の目標値が入っている", () => {
      renderRow(PAIRED);
      expect(screen.getByLabelText("y_axis の目標値")).toHaveValue(4);
    });

    it("目標値が無ければ現在値が入る", () => {
      renderRow({ ...PAIRED, target: null, value: 12.5 });
      expect(screen.getByLabelText("y_axis の目標値")).toHaveValue(12.5);
    });

    it("編集していない間はサーバーの目標値へ追従する", () => {
      const { view } = renderRow(PAIRED);
      view.rerender(
        <ManualAxisRow
          axis={{ ...PAIRED, target: 20 }}
          blockedReason={null}
          selected={false}
          onSelect={vi.fn()}
          onJog={vi.fn()}
          onSet={vi.fn()}
          onMove={vi.fn()}
        />,
      );
      expect(screen.getByLabelText("y_axis の目標値")).toHaveValue(20);
    });

    it("編集中は追従しない (打っている最中に値が書き換わらない)", async () => {
      const user = userEvent.setup();
      const { view } = renderRow(PAIRED);
      const input = screen.getByLabelText("y_axis の目標値");

      await user.clear(input);
      await user.type(input, "1");
      view.rerender(
        <ManualAxisRow
          axis={{ ...PAIRED, target: 20 }}
          blockedReason={null}
          selected={false}
          onSelect={vi.fn()}
          onJog={vi.fn()}
          onSet={vi.fn()}
          onMove={vi.fn()}
        />,
      );

      expect(screen.getByLabelText("y_axis の目標値")).toHaveValue(1);
    });

    it("範囲外を打ったら丸められる先を送信前に伝える", async () => {
      const user = userEvent.setup();
      renderRow(PAIRED);
      const input = screen.getByLabelText("y_axis の目標値");

      await user.clear(input);
      await user.type(input, "999");

      expect(screen.getByText(/範囲外/)).toHaveTextContent("20 mm へ丸めます");
      expect(screen.getByLabelText("y_axis を入力値へ移動")).toBeEnabled();
    });

    it("範囲内なら警告を出さない", async () => {
      const user = userEvent.setup();
      renderRow(PAIRED);
      const input = screen.getByLabelText("y_axis の目標値");

      await user.clear(input);
      await user.type(input, "10");

      expect(screen.queryByText(/範囲外/)).toBeNull();
    });
  });

  describe("左右のずれ", () => {
    it("平常時は数値だけを静かに出す", () => {
      renderRow(PAIRED);
      const shown = screen.getByText(/ずれ/);
      expect(shown).toHaveTextContent("ずれ 0.20 mm");
      expect(shown.closest(".badge")).toBeNull();
    });

    it("揃っていることと測れていないことを区別する", () => {
      renderRow({ ...PAIRED, deviation: 0 });
      expect(screen.getByText(/ずれ/)).toHaveTextContent("ずれ 0.00 mm");
    });

    it("許容差へ近づいたら自分から主張する", () => {
      renderRow({ ...PAIRED, deviation: 1.5, sync_tolerance: 2.0 });
      expect(screen.getByText(/ずれ/).closest(".badge")).not.toBeNull();
    });

    it("ずれようのない軸には何も出さない", () => {
      renderRow(DISCRETE);
      expect(screen.queryByText(/ずれ/)).toBeNull();
    });
  });

  describe("キーボード", () => {
    it("選択中の行は矢印キーでジョグできる", async () => {
      const user = userEvent.setup();
      const { onJog } = renderRow(PAIRED, null, true);

      await user.keyboard("{ArrowRight}");
      await user.keyboard("{ArrowLeft}");

      expect(onJog.mock.calls).toEqual([
        ["y_axis", 0.5],
        ["y_axis", -0.5],
      ]);
    });

    it("選択されていない行はキーで動かない", async () => {
      const user = userEvent.setup();
      const { onJog } = renderRow(PAIRED, null, false);

      await user.keyboard("{ArrowRight}");

      expect(onJog).not.toHaveBeenCalled();
    });

    it("操作が塞がれていればキーでも動かない", async () => {
      const user = userEvent.setup();
      const { onJog } = renderRow(PAIRED, "緊急停止中", true);

      await user.keyboard("{ArrowRight}");

      expect(onJog).not.toHaveBeenCalled();
    });

    it("Shift+Home / Shift+End で可動範囲の端へ飛ぶ", async () => {
      const user = userEvent.setup();
      const { onSet } = renderRow(PAIRED, null, true);

      await user.keyboard("{Shift>}{End}{/Shift}");
      await user.keyboard("{Shift>}{Home}{/Shift}");

      expect(onSet.mock.calls).toEqual([
        ["y_axis", 20],
        ["y_axis", -2],
      ]);
    });

    it("修飾キー無しの Home / End では 1 通も送らない", async () => {
      const user = userEvent.setup();
      const { onSet } = renderRow(PAIRED, null, true);

      await user.keyboard("{End}");
      await user.keyboard("{Home}");

      expect(onSet).not.toHaveBeenCalled();
    });

    it("[ ] でジョグ量を変える", async () => {
      const user = userEvent.setup();
      const { onJog } = renderRow(PAIRED, null, true);

      await user.keyboard("]");
      await user.keyboard("{ArrowRight}");

      expect(onJog).toHaveBeenCalledWith("y_axis", 2);
    });

    it("目標値を打っている間は矢印キーで機体が動かない", async () => {
      const user = userEvent.setup();
      const { onJog } = renderRow(PAIRED, null, true);

      await user.click(screen.getByLabelText("y_axis の目標値"));
      await user.keyboard("{ArrowLeft}");

      expect(onJog).not.toHaveBeenCalled();
    });
  });

  describe("連続操作を許していない軸", () => {
    it("ジョグも絶対値入力も出さない", () => {
      renderRow(DISCRETE);
      expect(screen.queryByLabelText(/進める$/)).toBeNull();
      expect(screen.queryByLabelText("gripper の目標値")).toBeNull();
    });

    it("プリセットは押せる (既定義の点しか送らないため)", async () => {
      const user = userEvent.setup();
      const { onMove } = renderRow(DISCRETE);

      await user.click(screen.getByLabelText("gripper を closed へ"));

      expect(onMove).toHaveBeenCalledWith("gripper", "closed");
    });

    it("プリセットは配信された位置名からしか作らない", () => {
      renderRow(DISCRETE);
      expect(screen.getByLabelText("gripper を open へ")).toBeInTheDocument();
      expect(screen.getByLabelText("gripper を closed へ")).toBeInTheDocument();
      expect(screen.queryByLabelText("gripper を half へ")).toBeNull();
    });
  });

  describe("プリセットの位置", () => {
    it("可動範囲バーに刻みとして出る", () => {
      renderRow(PAIRED);

      expect(screen.getByTitle("home 0 mm")).toBeInTheDocument();
      expect(screen.getByTitle("work 15 mm")).toBeInTheDocument();
    });

    it("刻みは可動範囲に対する比で置く", () => {
      renderRow(PAIRED);

      expect(screen.getByTitle("work 15 mm")).toHaveStyle({ left: "77.27272727272727%" });
    });

    it("値が読めなかったプリセットは描かない (0 へ寄せない)", () => {
      renderRow({
        ...PAIRED,
        positions: [
          { name: "home", value: 0 },
          { name: "work", value: null },
        ],
      });

      expect(screen.getByTitle("home 0 mm")).toBeInTheDocument();
      expect(screen.queryByTitle(/^work/)).toBeNull();
      expect(screen.getByLabelText("y_axis を work へ")).toBeInTheDocument();
    });

    it("連続軸ではボタンにも値を出す (バーの刻みと同じ場所を指す)", () => {
      renderRow(PAIRED);

      expect(screen.getByLabelText("y_axis を work へ")).toHaveAttribute("title", "15 mm");
    });

    it("離散状態の軸では値を出さない", () => {
      renderRow(DISCRETE);

      expect(screen.getByLabelText("gripper を open へ")).not.toHaveAttribute("title");
    });
  });

  describe("位置を測れない軸", () => {
    it("現在値を 0 で埋めない", () => {
      renderRow(DUTY);
      expect(screen.queryByText(/0\.00/)).toBeNull();
    });

    it("「現在」の欄そのものを出さない (常に「—」が並ぶだけの欄になる)", () => {
      renderRow(DUTY);
      expect(screen.queryByText("現在")).toBeNull();
      expect(screen.getByText("目標")).toBeInTheDocument();
    });

    it("測れる軸では「現在」を出す (畳んだ行でも落とさない)", () => {
      renderRow(DISCRETE);
      expect(screen.getByText("現在").parentElement).toHaveTextContent("5.00 deg");
    });

    it("位置軸の値が一時的に読めなくても「現在 —」は残す", () => {
      renderRow({ ...DISCRETE, value: null });
      expect(screen.getByText("現在").parentElement).toHaveTextContent("—");
    });

    it("duty 軸でもプリセットは送れる", async () => {
      const user = userEvent.setup();
      const { onMove } = renderRow(DUTY);
      await user.click(screen.getByLabelText("conveyor を run へ"));
      expect(onMove).toHaveBeenCalledWith("conveyor", "run");
    });

    it("duty の目標は単位付きの数値のまま", () => {
      renderRow({ ...DUTY, target: 0.95 });
      expect(screen.getByText("目標").parentElement).toHaveTextContent("0.95 duty");
    });
  });

  describe("離散状態 (on_off) の軸", () => {
    it("開指令は「ON」。1.00 とも on_off とも書かない", () => {
      renderRow({ ...VALVE, target: 1.0 });

      const target = screen.getByText("目標").parentElement;
      expect(target).toHaveTextContent("ON");
      expect(target).not.toHaveTextContent("1.00");
      expect(target).not.toHaveTextContent("on_off");
    });

    it("閉指令は「OFF」。0 を「未指令」と混ぜない", () => {
      renderRow({ ...VALVE, target: 0.0 });

      const target = screen.getByText("目標").parentElement;
      expect(target).toHaveTextContent("OFF");
      expect(target).not.toHaveTextContent("—");
    });

    it("一度も指令していなければ「—」", () => {
      renderRow(VALVE);
      expect(screen.getByText("目標").parentElement).toHaveTextContent("—");
    });
  });

  describe("操作できないとき", () => {
    it("全操作が塞がれる", () => {
      renderRow(PAIRED, "緊急停止中");
      expect(screen.getByLabelText("y_axis を 0.5mm 進める")).toBeDisabled();
      expect(screen.getByLabelText("y_axis の目標値")).toBeDisabled();
      expect(screen.getByLabelText("y_axis を home へ")).toBeDisabled();
      expect(screen.getByLabelText("y_axis を上限 20mm へ")).toBeDisabled();
    });
  });

  describe("モータ単位の操作面を作らない", () => {
    it("左右のモータを個別に動かすボタンが無い", () => {
      renderRow(PAIRED);
      for (const motor of PAIRED.motors) {
        expect(screen.queryByRole("button", { name: new RegExp(motor) })).toBeNull();
      }
      expect(screen.getByText("y_axis_r / y_axis_l")).toBeInTheDocument();
    });
  });
});
