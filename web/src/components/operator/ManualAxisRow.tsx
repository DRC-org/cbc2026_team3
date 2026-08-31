import { ChevronsLeft, ChevronsRight, Minus, Plus, Send } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { useHoldKey } from "@/hooks/useHoldKey";
import { useHoldRepeat } from "@/hooks/useHoldRepeat";
import { useHotkeys } from "@/hooks/useHotkeys";
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

/** 入力欄へ置く初期値。`5.00` ではなく `5` にして、打ち直しの手数を増やさない */
function toDraft(value: number | null): string {
  return value === null ? "" : String(Number(value.toFixed(3)));
}

/** 現在値の可動範囲内での位置 [%]。範囲外は端で止める */
function ratio(value: number, min: number, max: number): number {
  return Math.min(100, Math.max(0, ((value - min) / (max - min)) * 100));
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

interface ContinuousControlsProps {
  axis: ManualAxis;
  min: number;
  max: number;
  steps: number[];
  disabled: boolean;
  selected: boolean;
  onJog: (axis: string, delta: number) => void;
  onSet: (axis: string, value: number) => void;
}

/**
 * 押し続けたときに 1 回の量を何倍まで伸ばしてよいか。
 *
 * **可動域の 1/8 を 1 回で飛び越えない**ことを上限に置く。ベンチの `y_axis` は
 * 可動範囲 370mm / 刻み 10mm で、等倍のままでは端から端まで 37 回の押下が要る
 * (押しっぱなしでも 3 秒以上)。一方で刻みが青天井に伸びると、離す直前の 1 回だけで
 * 機構を端まで運んでしまう。2 の冪へ落とすのは、伸びた量を `×2` `×4` として
 * 画面に出したときに操縦者が暗算できる形にするため。
 */
function maxMultiplierFor(min: number, max: number, step: number): number {
  const limit = (max - min) / 8 / step;
  if (!Number.isFinite(limit) || limit < 2) return 1;
  return 2 ** Math.floor(Math.log2(limit));
}

function ContinuousControls({
  axis,
  min,
  max,
  steps,
  disabled,
  selected,
  onJog,
  onSet,
}: ContinuousControlsProps) {
  // **インデックスで扱う。** かつては `<option value={候補の数値}>` に戻して
  // `steps.indexOf(Number(...))` で引き直していたが、config の `steps` に浮動小数
  // (0.05 等) が入ると往復変換で一致せず `indexOf` が -1 を返し、刻みが**黙って 1**
  // へ落ちた。症状は「選んだ量と違う量で動く」だけで、config にも画面にも痕跡が残らない。
  // 可動範囲と刻みは config が宣言する境界であって、UI が値を捏造してよい所ではない
  const [stepIndex, setStepIndex] = useState(0);
  const step = steps[Math.min(stepIndex, steps.length - 1)];
  const maxMultiplier = maxMultiplierFor(min, max, step);

  // 端に達したら押しても動かない。理由を画面から読めるようにボタン側で塞ぐ
  // (実際のクランプはサーバーが行う。ここは説明であって判定ではない)
  const anchor = axis.target ?? axis.value;
  const atMin = anchor !== null && anchor <= min;
  const atMax = anchor !== null && anchor >= max;

  const canMinus = !disabled && !atMin;
  const canPlus = !disabled && !atMax;
  const jog = (sign: 1 | -1) => (multiplier: number) => onJog(axis.name, sign * step * multiplier);

  const minus = useHoldRepeat(jog(-1), canMinus, maxMultiplier);
  const plus = useHoldRepeat(jog(1), canPlus, maxMultiplier);
  // キーボードは選択中の行だけが張る。押しっぱなしの加速はポインタと同じ engine
  const keyMinus = useHoldKey("ArrowLeft", jog(-1), selected && canMinus, maxMultiplier);
  const keyPlus = useHoldKey("ArrowRight", jog(1), selected && canPlus, maxMultiplier);

  const minusBoost = Math.max(minus.multiplier, keyMinus.multiplier);
  const plusBoost = Math.max(plus.multiplier, keyPlus.multiplier);

  useHotkeys(
    {
      "[": () => setStepIndex((i) => Math.max(0, i - 1)),
      "]": () => setStepIndex((i) => Math.min(steps.length - 1, i + 1)),
      Home: () => onSet(axis.name, min),
      End: () => onSet(axis.name, max),
    },
    selected && !disabled,
  );

  return (
    <>
      <RangeBar axis={axis} min={min} max={max} />

      {/* クラスタ単位で折り返す。要素ごとに折り返させると、軸によって
          「+ が右端にある行」と「+ が次の行の左端にある行」が混在し、
          同じ操作を毎回探し直すことになる */}
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <div className="flex items-center gap-1">
          <Button
            disabled={disabled || atMin}
            onClick={() => onSet(axis.name, min)}
            aria-label={`${axis.name} を下限 ${min}${axis.unit} へ`}
            title={`下限 ${min} ${axis.unit}`}
          >
            <Icon as={ChevronsLeft} />
          </Button>
          <Button
            disabled={!canMinus}
            aria-label={`${axis.name} を ${step * minusBoost}${axis.unit} 戻す`}
            {...minus.handlers}
          >
            <Icon as={Minus} />
            <Boost multiplier={minusBoost} />
          </Button>
          <select
            className="select w-24 border-base-300 bg-base-100 font-mono tabular-nums select-sm"
            aria-label={`${axis.name} のジョグ量`}
            value={stepIndex}
            disabled={disabled}
            onChange={(e) => setStepIndex(Number(e.target.value))}
          >
            {steps.map((candidate, index) => (
              <option key={candidate} value={index}>
                {candidate} {axis.unit}
              </option>
            ))}
          </select>
          <Button
            disabled={!canPlus}
            aria-label={`${axis.name} を ${step * plusBoost}${axis.unit} 進める`}
            {...plus.handlers}
          >
            <Icon as={Plus} />
            <Boost multiplier={plusBoost} />
          </Button>
          <Button
            disabled={disabled || atMax}
            onClick={() => onSet(axis.name, max)}
            aria-label={`${axis.name} を上限 ${max}${axis.unit} へ`}
            title={`上限 ${max} ${axis.unit}`}
          >
            <Icon as={ChevronsRight} />
          </Button>
        </div>

        <AbsoluteEntry axis={axis} min={min} max={max} disabled={disabled} onSet={onSet} />
      </div>
    </>
  );
}

/**
 * 押し続けて伸びた実効量。**伸びていないときは何も出さない** ——
 * 常時 `×1` を出すと、平常時の行に意味の無い記号が 2 つ増える。
 */
function Boost({ multiplier }: { multiplier: number }) {
  if (multiplier <= 1) return null;
  return <span className="font-mono text-[0.8em] tabular-nums">×{multiplier}</span>;
}

/**
 * 可動範囲の表示。**ドラッグできる入力にしてはならない** ——
 * スライダーにすると、掴んだ瞬間に機体がその位置へ飛ぶ。
 *
 * 現在値と目標値は太さと色で描き分ける。同じ 2px の線で描いていた頃は、
 * 両者が近いと重なって「追従が遅れているのか、目標がそこなのか」が読めなかった。
 */
function RangeBar({ axis, min, max }: { axis: ManualAxis; min: number; max: number }) {
  const valuePct = axis.value === null ? null : ratio(axis.value, min, max);
  const targetPct = axis.target === null ? null : ratio(axis.target, min, max);

  return (
    <div className="flex items-center gap-2 text-[0.8em] text-base-content/55">
      <span className="w-16 shrink-0 text-right font-mono tabular-nums">
        {min} {axis.unit}
      </span>
      <div className="relative h-2.5 min-w-0 flex-1 bg-base-200">
        {/* 現在値から目標値までの移動ぶん。どちらへ向かっているかを面で示す */}
        {valuePct === null || targetPct === null ? null : (
          <span
            className="absolute top-0 h-full bg-info/15"
            style={{
              left: `${Math.min(valuePct, targetPct)}%`,
              width: `${Math.abs(targetPct - valuePct)}%`,
            }}
            aria-hidden
          />
        )}
        {targetPct === null ? null : (
          <span
            className="absolute top-0 h-full w-1 -translate-x-1/2 bg-info/70"
            style={{ left: `${targetPct}%` }}
            aria-hidden
          />
        )}
        {valuePct === null ? null : (
          <span
            className="absolute top-0 h-full w-0.5 -translate-x-1/2 bg-base-content/80"
            style={{ left: `${valuePct}%` }}
            aria-hidden
          />
        )}
      </div>
      <span className="w-16 shrink-0 font-mono tabular-nums">
        {max} {axis.unit}
      </span>
    </div>
  );
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
 * 送信後に `dirty` を落とすので、サーバーが範囲外をクランプしたときは
 * 返ってきた目標値が入力欄に入り、丸められたことが画面に残る。
 */
function AbsoluteEntry({ axis, min, max, disabled, onSet }: AbsoluteEntryProps) {
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
