import { StatusBadge } from "@/components/ui/StatusBadge";
import { cx } from "@/lib/cx";
import { motorTempTone } from "@/lib/healthVerdict";
import type { TempThresholds } from "@/lib/healthVerdict";
import { MALFORMED, readCommand, readMeasured } from "@/lib/protocol";
import type {
  Malformed,
  Measured,
  MotorHealth,
  MotorHealthState,
  MotorState,
} from "@/lib/protocol";
import { formatAge } from "@/lib/time";
import type { Tone } from "@/lib/tone";
import { TONE_TEXT_CLASS } from "@/lib/tone";

interface MotorStatusProps {
  name: string;
  state: MotorState;
  health?: MotorHealth;
  /** 温度の色分けに使うしきい値。サーバー由来で、MotorSummary から流れてくる */
  tempThresholds?: TempThresholds | null;
  className?: string;
}

const HEALTH_TONE: Record<MotorHealthState, Tone> = {
  ok: "success",
  stale: "warning",
  warning: "warning",
  fault: "error",
};

/**
 * 温度帯に応じた文字色。正常域は地の文字色のままにする
 * (全モータが緑に光ると、本当に見るべき 1 基が沈む)。
 * しきい値が未取得なら `motorTempTone` が neutral を返すので、色は付かない。
 *
 * **温度を測れないモータ (null) にも色を付けない。** 適当な既定値で「正常」とも
 * 「警告」とも言わないのは、しきい値未配信のときと同じ扱い。
 */
function tempTextClass(temp: Measured, thresholds: TempThresholds | null): string {
  const tone = motorTempTone(temp, thresholds);
  return tone === "warning" || tone === "error" ? cx(TONE_TEXT_CLASS[tone], "font-medium") : "";
}

/** 測る手段が無い項目の表示。桁位置は数値と同じグリッドに載る */
const UNMEASURED = "—";

/** 読めなかった配信の印。`—` (測れない) と同じ記号にしてはならない */
const UNREADABLE = "?";

/** 実測値ではなく指令値であることの印。数値の前に置く */
const COMMAND_MARK = "→";

/**
 * 指令値であって実出力ではないことを、記号だけに頼らず言葉でも出す。
 *
 * 幅 300px の診断カラムに凡例を置く余地は無いので、ホバー (title) に載せる。
 * **「送った」と「出ている」の差はここでしか説明できない** —— PC は送った値しか
 * 知らず、ファームの `max_duty` クランプ・`everFed_` ゲート・ウォッチドッグ満了・
 * 緊急停止ラッチのどれでも実出力は 0 になりうるが、この欄の値は変わらない。
 */
const COMMAND_TITLE =
  "PC が最後に送った指令値です（実際の出力ではありません）。この基板は出力を測る手段を持たないため、緊急停止・ウォッチドッグ満了・ファーム側の上限クランプで基板が出していなくても、ここには値が残ります。";

/**
 * 指令値 1 つの表示文字列。**丸め方は `command_mode` だけで決める** ——
 * モータ名や基板の種類から推測すると、ドライバ種別を UI へ書き写すことになる。
 *
 * `on_off` は電磁弁の開閉指令で、基板は 0 か非 0 かしか見ない。0.0 / 1.0 と
 * 数字で出すと duty と見分けが付かないので `ON` / `OFF` と書く。
 */
function commandText(value: number, mode: string | null): string {
  if (mode === "on_off") return value === 0 ? "OFF" : "ON";
  // duty は 0.30 のような値なので 1 桁では 0.3 と 0.34 が同じに見える
  return mode === "duty" ? value.toFixed(2) : value.toFixed(1);
}

/** 4 値の桁位置をモータ間で揃えるためのグリッド。ヘッダーと値行で共有する */
const STAT_GRID_CLASS = "grid grid-cols-4 gap-1 px-1 text-right";

const STAT_LABELS = ["POS", "VEL", "TRQ", "TMP"];

/**
 * 数値列の見出し。モータ 1 基ごとに `POS` `VEL` などを併記すると、
 * 幅 300px の診断カラムでは値の桁数しだいでラベルが `OS` まで削られて読めなくなる。
 * 見出しは一覧に 1 行だけ置き、各行は数値だけを並べる。
 */
export function MotorStatHeader({ className }: { className?: string }) {
  return (
    <div className={cx(STAT_GRID_CLASS, "text-[0.8em] text-base-content/60", className)}>
      {STAT_LABELS.map((label) => (
        <span key={label}>{label}</span>
      ))}
    </div>
  );
}

/**
 * 数値 1 つ。**測れない項目に `0` を描いてはならない** —— 本当に 0 なのか
 * 測る手段が無いのかを操縦者が区別できなくなる (DC 基板・電磁弁基板は
 * エンコーダも電流センスも温度センサも積んでいない)。
 *
 * 単位は値があるときだけ付ける。`—℃` は意味を持たない。
 *
 * `MALFORMED` (欄の欠落・型違い) だけは異常側へ倒して `?` を出す。`—` と同じ
 * 記号にすると、配信の不具合が「測れない」として画面から消える。
 */
function Cell({
  value,
  unit,
  toneClass,
}: {
  value: Measured | Malformed;
  unit?: string;
  toneClass?: string;
}) {
  if (value === MALFORMED) {
    return (
      <span className={cx("truncate font-mono tabular-nums", TONE_TEXT_CLASS.error)}>
        {UNREADABLE}
      </span>
    );
  }
  if (value === null) {
    return (
      <span className="truncate font-mono text-base-content/50 tabular-nums">{UNMEASURED}</span>
    );
  }
  return (
    <span className={cx("truncate font-mono tabular-nums", toneClass)}>
      {value.toFixed(1)}
      {unit ? <span className="text-base-content/60">{unit}</span> : null}
    </span>
  );
}

/**
 * POS 欄。**位置を測れるモータでは実測値だけを出す。**
 *
 * 実測値がある行に指令値を併記しない —— 同じ事実を 2 度描かない原則もあるが、
 * それ以上に、この欄が実測なのか指令なのかを行ごとに読み分けさせる形になる。
 *
 * 位置を測れないモータ (DC 基板の duty・電磁弁基板の on_off) だけが、代わりに
 * **PC が最後に送った指令値**を出す。**これは実出力ではない** (`MotorState.command`
 * の説明を参照)。実測との取り違えを防ぐのが `→` と `title` の 2 つで、記号だけでは
 * 「送った値」と「出ている値」の差までは伝わらない。
 */
function PositionCell({ state }: { state: MotorState }) {
  const measured = readMeasured(state.pos);
  if (measured !== null) return <Cell value={measured} />;

  const commanded = readCommand(state.command);
  // 一度も指令していない / 緊急停止で目標値を捨てた場合は素直に「無い」と出す
  if (commanded === null || commanded === MALFORMED) return <Cell value={commanded} />;

  // 型は実行時に消えるので、丸め方を決める前に文字列であることを確かめる
  const mode = typeof state.command_mode === "string" ? state.command_mode : null;
  return (
    <span className="truncate font-mono tabular-nums" title={COMMAND_TITLE}>
      <span className="text-base-content/50">{COMMAND_MARK}</span>
      {commandText(commanded, mode)}
    </span>
  );
}

export function MotorStatus({
  name,
  state,
  health,
  tempThresholds = null,
  className,
}: MotorStatusProps) {
  // 温度だけは色分けにも使うので、読み取りを 1 度で済ませる
  const temp = readMeasured(state.temp);

  // モータ名と数値を同じ行に並べると、サイドカラム幅ではモータ名が "li..." まで
  // 削られて識別できなくなる。名前を独立した行に出して常に読めるようにする
  return (
    <div className={cx("flex flex-col py-[0.15rem]", className)}>
      <div className="flex min-w-0 items-center justify-between gap-2 px-1">
        <span className="min-w-0 truncate font-medium">{name}</span>
        {health ? (
          <span className="flex shrink-0 items-center gap-1.5 whitespace-nowrap">
            <StatusBadge tone={HEALTH_TONE[health.state]}>{health.state.toUpperCase()}</StatusBadge>
            <span className="text-[0.8em] text-base-content/60">
              {formatAge(health.feedback_age_ms)}
            </span>
          </span>
        ) : null}
      </div>
      {/* 見出しは MotorStatHeader が一覧に 1 行だけ出す。同じグリッドを使って桁位置を揃える */}
      <div className={STAT_GRID_CLASS}>
        {/* 指令値を出すのは POS 欄だけ。速度も電流も温度も PC は指令していないので、
            測れない残り 3 欄は常に「—」のまま (埋めると「測ったように見える値」になる) */}
        <PositionCell state={state} />
        <Cell value={readMeasured(state.vel)} />
        <Cell value={readMeasured(state.torque)} />
        <Cell
          value={temp}
          unit="℃"
          toneClass={tempTextClass(temp === MALFORMED ? null : temp, tempThresholds)}
        />
      </div>
      {/*
        ドライバからの補足。平常時は null なので 1px も占めない。
        状態バッジ (WARNING) が目を引く役、この行が「何をすればよいか」を伝える役で、
        色は付けない —— 状態はチップが示す約束なので、着色テキストを重ねない
      */}
      {health?.detail ? (
        <p className="px-1 pt-0.5 text-[0.8em] leading-snug text-base-content/80">
          {health.detail}
        </p>
      ) : null}
    </div>
  );
}
