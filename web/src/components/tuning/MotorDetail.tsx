import { CircleHelp, Link2, Send, Undo2 } from "lucide-react";

import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { Panel } from "@/components/ui/Panel";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { cx } from "@/lib/cx";
import { motorTempTone } from "@/lib/healthVerdict";
import type { TempThresholds } from "@/lib/healthVerdict";
import { PID_PARAMS, PID_STEP, deviationOf } from "@/lib/pidTuning";
import type { PidKey, TuningEntry } from "@/lib/pidTuning";

/** 調整対象の 1 値を大きく出す。応答を見ながら詰める作業なので視認性を優先する */
function Readout({
  label,
  value,
  unit,
  emphasize,
}: {
  label: string;
  value: string;
  unit?: string;
  /** 偏差だけは他より強く出す。調整で見たい量そのもの */
  emphasize?: boolean;
}) {
  return (
    <div className="flex min-w-0 flex-col items-end">
      <span className="text-[0.8em] text-base-content/60">{label}</span>
      <span
        className={cx(
          "font-mono text-[1.5em] leading-tight tabular-nums",
          emphasize && "font-medium text-info",
        )}
      >
        {value}
        {unit ? <span className="text-[0.7em] text-base-content/60">{unit}</span> : null}
      </span>
    </div>
  );
}

/** 測っていない値は「—」。数字で埋めると、測った値と区別が付かなくなる */
function fixed1(value: number | null): string {
  return value === null ? "—" : value.toFixed(1);
}

interface PidRowProps {
  label: string;
  max: number;
  value: number;
  onChange: (val: number) => void;
}

/**
 * 入力欄へ出す文字列。桁を揃えず、いま効いている値をそのまま見せる。
 *
 * 固定小数点で丸めると `kd: 0.125` が `0.13` と表示され、画面の値と機体の値が
 * 食い違う。二進小数のゴミ (0.30000000000000004) だけを落とす。
 */
function display(value: number): string {
  return String(Math.round(value * 1e6) / 1e6);
}

// PID 1 項目の行: 数値入力 + レンジ。送信は行ごとではなく下の「送信」に集約する。
function PidRow({ label, max, value, onChange }: PidRowProps) {
  // クランプ後に PID_STEP 単位の浮動小数誤差を丸める。
  const clamp = (val: number) => {
    if (!Number.isFinite(val)) return 0;
    const next = Math.min(max, Math.max(0, val));
    return Math.round(next / PID_STEP) * PID_STEP;
  };

  return (
    <div className="flex items-center gap-3">
      <label className="w-8 shrink-0 font-mono text-base-content/70" htmlFor={`pid-${label}`}>
        {label}
      </label>
      {/* スライダーだけだと 0.01 刻みで狙った値に置けない。直接入力を主にする */}
      <input
        id={`pid-${label}`}
        type="number"
        className="input w-24 shrink-0 border-base-300 bg-base-100 text-right font-mono tabular-nums input-sm"
        min={0}
        max={max}
        step={PID_STEP}
        value={display(value)}
        onChange={(e) => onChange(clamp(Number(e.target.value)))}
      />
      <input
        type="range"
        className="range min-w-0 flex-1 text-info [--range-thumb:var(--color-info)] range-xs"
        aria-label={`${label} スライダー`}
        min={0}
        max={max}
        step={PID_STEP}
        value={value}
        onChange={(e) => onChange(clamp(Number(e.target.value)))}
      />
      <span className="w-10 shrink-0 text-right text-[0.85em] text-base-content/50 tabular-nums">
        {max}
      </span>
    </div>
  );
}

/**
 * 選択中 1 基の現在値と PID 入力。
 *
 * ページ本体から切り出してあるのは、この画面が「モータ一覧 + 詳細 + 応答」の
 * 3 部構成で、それぞれが独立して読めるため。
 */
export function MotorDetail({
  entry,
  getValue,
  setValue,
  onSend,
  edited,
  onDiscard,
  blockedReason,
  tempThresholds,
}: {
  entry: TuningEntry;
  getValue: (entry: TuningEntry, param: PidKey) => number;
  setValue: (entry: TuningEntry, param: PidKey, val: number) => void;
  onSend: () => void;
  /** 未送信の編集を抱えているか。抱えている間だけ機体の実ゲインを併記する */
  edited: boolean;
  /** 編集を捨てて配信値へ戻す */
  onDiscard: () => void;
  /** 送信できない理由。null なら送れる */
  blockedReason: string | null;
  /** 温度の色分けに使うしきい値。正はサーバーの config (`server_info` で届く) */
  tempThresholds: TempThresholds | null;
}) {
  const { motor, motorState, pid } = entry;
  // 左右直結ペアはサーバーがグループ全員へ同じ値を入れる (片側だけ別特性にすると
  // 押し合って機構が壊れる)。適用先はサーバーが配る `applies_to` をそのまま描く
  const paired = pid.applies_to.length > 1;

  return (
    <Panel
      legend={`${entry.robotLabel} / ${motor}`}
      actions={
        <StatusBadge tone={motorTempTone(motorState.temp, tempThresholds)}>
          {motorState.temp.toFixed(1)}℃
        </StatusBadge>
      }
    >
      {/* 応答を見ながら詰めるので、選択中 1 基の現在値は大きく出す。
          **偏差と飽和がこの行の主役。** 以前は POS/VEL/TORQUE/TEMP の 4 つしか
          無く、調整で最も見たい「目標からどれだけ外れているか」が画面のどこにも
          存在しなかった (操縦者の頭の中の引き算にしかなかった) */}
      <div className="flex shrink-0 justify-between gap-4 border-b border-base-300 pb-2">
        <Readout label="POS" value={motorState.pos.toFixed(1)} />
        <Readout label="TARGET" value={fixed1(motorState.target)} />
        <Readout label="ERROR" value={fixed1(deviationOf(motorState))} emphasize />
        <Readout label="TORQUE" value={motorState.torque.toFixed(2)} />
        <Readout label="TEMP" value={motorState.temp.toFixed(1)} unit="℃" />
      </div>

      {/* 飽和は平常時に出さない。**異常時にだけ自分から主張する。**
          出力が上限に張り付いている間はゲインを変えても応答が変わらないので、
          これを知らずに kp を動かすと「効かない」という誤った結論に至る */}
      {motorState.saturated ? (
        <div className="mt-2 flex shrink-0 items-center gap-2">
          <StatusBadge tone="warning">出力が上限</StatusBadge>
          <span className="text-[0.9em] text-base-content/70">
            飽和している間はゲインを変えても応答は変わりません。
          </span>
        </div>
      ) : null}

      {paired ? (
        <div className="mt-3 flex shrink-0 items-center gap-1.5 text-[0.9em] text-base-content/70">
          <Icon as={Link2} />
          送信すると {pid.applies_to.join(" / ")} に適用されます（左右直結ペア）
        </div>
      ) : null}

      <div className="flex flex-col gap-3 pt-3">
        {PID_PARAMS.map(({ key, label, max }) => {
          const value = getValue(entry, key);
          return (
            <PidRow
              key={key}
              label={label}
              // config の値が作業レンジを超えていることがある。上限で切ると
              // 表示された値と機体の実際のゲインが食い違う
              max={Math.max(max, value)}
              value={value}
              onChange={(val) => setValue(entry, key, val)}
            />
          );
        })}
      </div>

      {/* **編集中は機体で実際に効いているゲインを併記する。** 入力欄は編集値を
          出すので、書き換えた瞬間から現在値が画面のどこにも無くなる。かつて
          「元の値へ戻す術が config を読むしかない」状態で全ゲイン 0 上書きの
          事故が起きており、編集後にそれが再現していた。**平常時 (未編集) は
          出さない** — 同じ 3 値が入力欄のすぐ上に並ぶだけになる */}
      {edited ? (
        <div className="mt-3 flex shrink-0 flex-wrap items-center gap-x-3 gap-y-1">
          <span className="text-base-content/70">
            適用中:{" "}
            <span className="font-mono tabular-nums">
              {PID_PARAMS.map(({ key, label }) => `${label} ${display(pid[key])}`).join(" / ")}
            </span>
          </span>
          <Button onClick={onDiscard} aria-label={`${motor} の編集を破棄して現在値へ戻す`}>
            <Icon as={Undo2} />
            現在値へ戻す
          </Button>
        </div>
      ) : null}

      {/* 送信は明示操作のみ。スライダーを触っただけでは set_param を飛ばさない。
          値の編集自体は塞がない (試合中に次の値を用意しておけるほうが実務に合う) */}
      <div className="mt-3 flex shrink-0 items-center gap-3">
        <Button
          tone="info"
          onClick={onSend}
          disabled={blockedReason !== null}
          aria-label={`${motor} の PID を送信`}
        >
          <Icon as={Send} />
          この 3 値を送信
        </Button>
        {blockedReason ? (
          <span className="flex items-center gap-1.5 text-base-content/70">
            <Icon as={CircleHelp} />
            {blockedReason}
          </span>
        ) : (
          <span className="text-base-content/60">スライダー操作だけでは送信されません</span>
        )}
      </div>
    </Panel>
  );
}
