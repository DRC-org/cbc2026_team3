import { Send } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { cx } from "@/lib/cx";
import type { ManualAxis } from "@/lib/protocol";

/** 入力欄へ置く初期値。`5.00` ではなく `5` にして、打ち直しの手数を増やさない */
function toDraft(value: number | null): string {
  return value === null ? "" : String(Number(value.toFixed(3)));
}

interface AbsoluteEntryProps {
  axis: ManualAxis;
  min: number;
  max: number;
  disabled: boolean;
  onSet: (axis: string, value: number) => void;
}

/**
 * 絶対値の指定。**入力欄には現在の目標値 (無ければ現在値) を入れておく。**
 *
 * 空欄始まりだと「今 12.5mm、15mm にしたい」でも毎回 4 打鍵が要り、
 * 大きく動かす唯一の手段が実質ジョグの連打だけになっていた。
 *
 * **追従させるのは `target` だけで、`value` は見ない。** 現在値は 20Hz で動くので、
 * 追わせると編集していない間ずっと数字が入れ替わり、入力欄として使えない。
 * **送信した瞬間に今の目標値へ戻す** (`dirty` を落として配信を待つのではない)。
 * 待つ形だと、既に端にいる軸へ範囲外を送ったときに `axis.target` が動かず、
 * 入力欄が範囲外の値のまま居座る。範囲内なら直後の配信が新しい目標値へ差し替え、
 * 範囲外ならサーバーが丸めた端の値が残るので、丸められたことは画面から読める。
 */
export function AbsoluteEntry({ axis, min, max, disabled, onSet }: AbsoluteEntryProps) {
  const [draft, setDraft] = useState(() => toDraft(axis.target ?? axis.value));
  const dirtyRef = useRef(false);
  // 初期値の材料。effect の依存に `value` を入れないための参照
  const seedRef = useRef<number | null>(null);
  seedRef.current = axis.target ?? axis.value;

  useEffect(() => {
    if (dirtyRef.current) return;
    setDraft(toDraft(seedRef.current));
  }, [axis.target]);

  const parsed = Number(draft);
  const filled = draft.trim() !== "" && Number.isFinite(parsed);
  // 範囲外は拒否ではなくクランプ (拒否だと端で操作そのものが効かなくなる)。
  // ここは送る前の説明であって判定ではない
  const outOfRange = filled && (parsed < min || parsed > max);

  const submit = () => {
    if (!filled) return;
    dirtyRef.current = false;
    // **その場で今の目標値へ戻す。** `dirty` を落として `axis.target` の変化に
    // 任せるだけだと、既に端 (max) にいる軸へ範囲外を送ったときに target が動かず
    // effect も走らないので、入力欄は範囲外の値のままオレンジ枠で居座る ——
    // 操縦者には「送ったのに反映されていない」としか見えない。
    // **クランプ後の値を自分で入れて確定させない** —— 実際に丸めるのはサーバーで、
    // その結果は次の配信で必ず届く。今の目標値を出して待てば、範囲内なら直後の配信が
    // 新しい値へ差し替え、範囲外なら丸められた結果 (= 端の値) が入る
    // (送る前の「{端の値} へ丸めます」は説明であって、欄へ確定させる値ではない)
    setDraft(toDraft(seedRef.current));
    onSet(axis.name, parsed);
  };

  return (
    <div className="flex items-center gap-1">
      <input
        type="number"
        inputMode="decimal"
        className={cx(
          "input w-24 border-base-300 bg-base-100 text-right font-mono tabular-nums input-sm",
          outOfRange && "border-warning",
        )}
        aria-label={`${axis.name} の目標値`}
        placeholder={axis.unit}
        value={draft}
        disabled={disabled}
        onChange={(e) => {
          dirtyRef.current = true;
          setDraft(e.target.value);
        }}
        onKeyDown={(e) => {
          if (e.key === "Enter") submit();
        }}
      />
      <Button
        tone="info"
        disabled={disabled || !filled}
        onClick={submit}
        aria-label={`${axis.name} を入力値へ移動`}
      >
        <Icon as={Send} />
        移動
      </Button>
      {outOfRange ? (
        <span className="text-[0.8em] text-warning">
          範囲外 — {parsed < min ? min : max} {axis.unit} へ丸めます
        </span>
      ) : null}
    </div>
  );
}
