import { useEffect, useRef } from "react";

import { ContinuousControls } from "@/components/operator/ContinuousControls";
import { Button } from "@/components/ui/Button";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { cx } from "@/lib/cx";
import type { ManualAxis } from "@/lib/protocol";
import { evaluateSync } from "@/lib/syncVerdict";

interface ManualAxisRowProps {
  axis: ManualAxis;
  /** 操作できない理由。null なら操作できる */
  blockedReason: string | null;
  /** キーボードの操作対象になっている軸か */
  selected: boolean;
  /** この行を操作対象にする */
  onSelect: () => void;
  onJog: (axis: string, delta: number) => void;
  onSet: (axis: string, value: number) => void;
  onMove: (axis: string, position: string) => void;
}

/** 読めない値の表示。0 で埋めない (測っていない値を測ったように見せない) */
function format(value: number | null, unit: string): string {
  return value === null ? "—" : `${value.toFixed(2)}${unit ? ` ${unit}` : ""}`;
}

/**
 * 手動操縦の 1 軸。**行の単位は論理軸であってモータではない。**
 *
 * モータ単位の操作面を作ってはならない。左右直結ペア (`y_axis` / `rotate`) が
 * 別々の時刻に動くとその場で機構が壊れるため、指令は必ず軸単位で 1 回だけ出す。
 * ここにモータ名を出しているのは「どの実体が動くか」を示すためだけで、
 * 個別に押せる要素にはしない。
 *
 * 見た目は軸の性格で 3 通りに分かれるが、分岐の根拠はサーバー配信の
 * `manual` と `command_mode` だけで、軸名は一切見ていない (機構が変わって
 * 軸が増減しても、この画面は何も変えなくてよい)。
 *
 * **キーボードの割り当ては選択中の行だけが張る。** 同じキーを全行が張ると、
 * どの軸へ飛ぶかが登録順という画面から読めない事情で決まる。
 */
export function ManualAxisRow({
  axis,
  blockedReason,
  selected,
  onSelect,
  onJog,
  onSet,
  onMove,
}: ManualAxisRowProps) {
  const range = axis.manual;
  const disabled = blockedReason !== null;
  const rowRef = useRef<HTMLDivElement>(null);

  // キーボードで選択を移したとき、畳まれた先が見えないと移動したことが分からない
  useEffect(() => {
    if (selected) rowRef.current?.scrollIntoView?.({ block: "nearest" });
  }, [selected]);

  return (
    <div
      ref={rowRef}
      // 選択中は左のアクセントで示す。行全体を押せるボタンにはしない
      // (行のどこを押しても何かが起きる面にすると、値を読むための視線移動が操作になる)
      className={cx(
        "flex flex-col gap-1 border-b border-base-300 border-l-[0.4rem] px-2 py-1.5 last:border-b-0",
        selected ? "border-l-info bg-base-200/40" : "border-l-transparent",
      )}
      onPointerDown={onSelect}
      onFocusCapture={onSelect}
    >
      <div className="flex min-w-0 flex-wrap items-baseline gap-x-3 gap-y-0.5">
        <span className="min-w-0 shrink-0 font-medium">{axis.name}</span>
        {/* 実体が軸名と同じ単一モータ軸では出さない。同じ語を 2 度描くだけで、
            「同じ事実を 2 度描かない」原則にも反する */}
        {axis.motors.length === 1 && axis.motors[0] === axis.name ? null : (
          <span className="shrink-0 text-[0.8em] text-base-content/45">
            {axis.motors.join(" / ")}
          </span>
        )}

        <SyncIndicator axis={axis} />

        <span className="ml-auto flex shrink-0 items-baseline gap-3 font-mono tabular-nums">
          <span className="text-[1.15em] font-medium">
            <span className="mr-1 font-sans text-[0.7em] font-normal text-base-content/55">
              現在
            </span>
            {format(axis.value, axis.unit)}
          </span>
          <span className="text-base-content/70">
            <span className="mr-1 font-sans text-[0.8em] text-base-content/55">目標</span>
            {format(axis.target, axis.unit)}
            <Delta value={axis.value} target={axis.target} />
          </span>
        </span>
      </div>

      {/* `steps` は config が「空にはならない」と保証する境界だが、空で届いたら
          刻みを 1 と捏造せず連続操作ごと出さない (捏造すると config に無い量が飛ぶ) */}
      {range && range.steps.length > 0 ? (
        <ContinuousControls
          axis={axis}
          min={range.min}
          max={range.max}
          steps={range.steps}
          disabled={disabled}
          selected={selected}
          onJog={onJog}
          onSet={onSet}
        />
      ) : null}

      {axis.positions.length > 0 ? (
        <div className="flex flex-wrap items-center gap-1">
          {/* プリセットは位置定数に定義された状態名からしか作らない。
              自由入力を許さないことで「定義した状態以外を送れない」保証が残る */}
          {axis.positions.map((position) => (
            <Button
              key={position}
              disabled={disabled}
              onClick={() => onMove(axis.name, position)}
              aria-label={`${axis.name} を ${position} へ`}
            >
              {position}
            </Button>
          ))}
        </div>
      ) : null}
    </div>
  );
}

/**
 * 目標までの残り。**現在値と目標値の引き算そのものは新しい事実ではない**が、
 * 「あと何 mm か」は操縦者が毎回頭でやっている計算で、桁を読み違えると
 * 行き過ぎに気付くのが 1 手遅れる。
 */
function Delta({ value, target }: { value: number | null; target: number | null }) {
  if (value === null || target === null) return null;
  const delta = target - value;
  if (Math.abs(delta) < 0.005) return null;
  return (
    <span className="ml-1 text-[0.8em] text-base-content/50">
      ({delta > 0 ? "+" : ""}
      {delta.toFixed(2)})
    </span>
  );
}

/**
 * 左右直結ペアのずれ。**平常時は静かにし、許容差へ近づいたときだけ主張する。**
 *
 * 数値そのものは常に出す。ここが空欄だと「揃っている」のか「測れていない」のかを
 * 画面から区別できず、`sync_tolerance` を詰める作業が勘になる。
 */
function SyncIndicator({ axis }: { axis: ManualAxis }) {
  const verdict = evaluateSync(axis);
  const { deviation, sync_tolerance: tolerance } = axis;
  // ずれようのない軸 (単独モータ) と測れない軸には語ることが無い
  if (typeof deviation !== "number") return null;

  const text = `ずれ ${deviation.toFixed(2)}${axis.unit ? ` ${axis.unit}` : ""}`;
  const title =
    typeof tolerance === "number" ? `許容差 ${tolerance.toFixed(2)} ${axis.unit}` : undefined;

  if (!verdict.alert) {
    return (
      <span
        className="shrink-0 font-mono text-[0.8em] text-base-content/45 tabular-nums"
        title={title}
      >
        {text}
      </span>
    );
  }
  return (
    <StatusBadge tone={verdict.tone} className="shrink-0" title={title}>
      {text}
    </StatusBadge>
  );
}
