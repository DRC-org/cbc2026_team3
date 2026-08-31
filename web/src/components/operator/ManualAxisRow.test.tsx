import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ManualAxisRow } from "@/components/operator/ManualAxisRow";
import type { ManualAxis } from "@/lib/protocol";

/** 左右直結ペアの連続軸 */
const PAIRED: ManualAxis = {
  name: "y_axis",
  unit: "mm",
  command_mode: "position",
  value: 5.0,
  target: 4.0,
  manual: { min: -2, max: 20, steps: [0.5, 2] },
  deviation: 0.2,
  sync_tolerance: 2.0,
  positions: ["home", "work"],
  motors: ["y_axis_r", "y_axis_l"],
};

/** 離散状態アクチュエータ (可動範囲を持たない) */
const DISCRETE: ManualAxis = {
  name: "gripper",
  unit: "deg",
  command_mode: "position",
  value: 5.0,
  target: null,
  manual: null,
  deviation: null,
  sync_tolerance: null,
  positions: ["open", "closed"],
  motors: ["gripper"],
};

/** 位置を測れない duty 軸 */
const DUTY: ManualAxis = {
  name: "conveyor",
  unit: "duty",
  command_mode: "duty",
  value: null,
  target: null,
  manual: null,
  deviation: null,
  sync_tolerance: null,
  positions: ["stop", "run"],
  motors: ["conveyor"],
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

      await user.selectOptions(screen.getByLabelText("y_axis のジョグ量"), "2");
      await user.click(screen.getByLabelText("y_axis を 2mm 進める"));

      expect(onJog).toHaveBeenCalledWith("y_axis", 2);
    });

    it("絶対値は入力して送信したときだけ飛ぶ", async () => {
      const user = userEvent.setup();
      const { onSet } = renderRow(PAIRED);
      const input = screen.getByLabelText("y_axis の目標値");

      await user.clear(input);
      await user.type(input, "12.5");
      // 入力しただけでは送らない (打っている途中の値で機体が動く)
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
      // スライダーにすると、触れた瞬間に機体が飛ぶ
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
      // 追従が遅れているあいだ現在値で判定すると、目標が既に端でも押せてしまう
      renderRow({ ...PAIRED, value: 0, target: 20 });
      expect(screen.getByLabelText("y_axis を 0.5mm 進める")).toBeDisabled();
    });
  });

  describe("大きく動かす", () => {
    it("可動範囲の端へ 1 回で飛べる", async () => {
      // これが無いと、可動域 22mm を刻み 0.5mm のジョグで渡ることになる。
      // 送る値は config が宣言した境界そのものなので、クランプ後と必ず一致する
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
      // 空欄始まりだと「今 4mm、7mm にしたい」でも毎回打ち直しになる
      renderRow(PAIRED);
      expect(screen.getByLabelText("y_axis の目標値")).toHaveValue(4);
    });

    it("目標値が無ければ現在値が入る", () => {
      renderRow({ ...PAIRED, target: null, value: 12.5 });
      expect(screen.getByLabelText("y_axis の目標値")).toHaveValue(12.5);
    });

    it("編集していない間はサーバーの目標値へ追従する", () => {
      // クランプされた値がここへ返るので、丸められたことが画面に残る
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
      // 拒否ではなくクランプ (拒否だと端で操作そのものが効かなくなる)。
      // ここは説明であって判定ではないので、送信自体は塞がない
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
      // 平常時に色付きチップを出すと、本当に見るべきときに沈む
      expect(shown.closest(".badge")).toBeNull();
    });

    it("揃っていることと測れていないことを区別する", () => {
      // 0 は正常な測定値。falsy 判定で捨てると最も健全な状態だけが空欄になる
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
      // 同じキーを全行が張ると、どの軸へ飛ぶかが登録順で決まってしまう
      const user = userEvent.setup();
      const { onJog } = renderRow(PAIRED, null, false);

      await user.keyboard("{ArrowRight}");

      expect(onJog).not.toHaveBeenCalled();
    });

    it("操作が塞がれていればキーでも動かない", async () => {
      const user = userEvent.setup();
      const { onJog } = renderRow(PAIRED, "緊急停止中は手動操縦できません", true);

      await user.keyboard("{ArrowRight}");

      expect(onJog).not.toHaveBeenCalled();
    });

    it("Home / End で可動範囲の端へ飛ぶ", async () => {
      const user = userEvent.setup();
      const { onSet } = renderRow(PAIRED, null, true);

      await user.keyboard("{End}");
      await user.keyboard("{Home}");

      expect(onSet.mock.calls).toEqual([
        ["y_axis", 20],
        ["y_axis", -2],
      ]);
    });

    it("[ ] でジョグ量を変える", async () => {
      const user = userEvent.setup();
      const { onJog } = renderRow(PAIRED, null, true);

      await user.keyboard("]");
      await user.keyboard("{ArrowRight}");

      expect(onJog).toHaveBeenCalledWith("y_axis", 2);
    });

    it("目標値を打っている間は矢印キーで機体が動かない", async () => {
      // 入力欄の ← → はカーソル移動であって、機体を動かす操作ではない
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
      // 定義に無い状態を送るボタンは存在しない
      expect(screen.queryByLabelText("gripper を half へ")).toBeNull();
    });
  });

  describe("位置を測れない軸", () => {
    it("現在値を 0 で埋めない", () => {
      // DC 基板はエンコーダを持たない。0 を出すと「測ったように見える 0」になる
      renderRow(DUTY);
      expect(screen.getByText(/現在/).parentElement).toHaveTextContent("—");
      expect(screen.queryByText(/0\.00 duty/)).toBeNull();
    });

    it("duty 軸でもプリセットは送れる", async () => {
      const user = userEvent.setup();
      const { onMove } = renderRow(DUTY);
      await user.click(screen.getByLabelText("conveyor を run へ"));
      expect(onMove).toHaveBeenCalledWith("conveyor", "run");
    });
  });

  describe("操作できないとき", () => {
    it("全操作が塞がれる", () => {
      renderRow(PAIRED, "緊急停止中は手動操縦できません");
      expect(screen.getByLabelText("y_axis を 0.5mm 進める")).toBeDisabled();
      expect(screen.getByLabelText("y_axis の目標値")).toBeDisabled();
      expect(screen.getByLabelText("y_axis を home へ")).toBeDisabled();
      expect(screen.getByLabelText("y_axis を上限 20mm へ")).toBeDisabled();
    });
  });

  describe("モータ単位の操作面を作らない", () => {
    it("左右のモータを個別に動かすボタンが無い", () => {
      // 左右直結ペアが別々の時刻に動くとその場で機構が壊れる。
      // モータ名は「どの実体が動くか」の表示にとどめ、押せる要素にはしない
      renderRow(PAIRED);
      for (const motor of PAIRED.motors) {
        expect(screen.queryByRole("button", { name: new RegExp(motor) })).toBeNull();
      }
      expect(screen.getByText("y_axis_r / y_axis_l")).toBeInTheDocument();
    });
  });
});
