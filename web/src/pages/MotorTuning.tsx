import { CircleHelp, Link2, Send } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { Page } from "@/components/ui/Page";
import { Panel } from "@/components/ui/Panel";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { useRobotCommands, useRobotStates, useRobotStatus } from "@/context/RobotContext";
import { cx } from "@/lib/cx";
import { motorTempTone } from "@/lib/healthVerdict";
import { isDuringMatch } from "@/lib/phase";
import type { MotorPid, MotorState } from "@/lib/protocol";
import { ROBOTS } from "@/lib/robots";

const PID_PARAMS = [
  { key: "kp", label: "Kp", max: 10 },
  { key: "ki", label: "Ki", max: 5 },
  { key: "kd", label: "Kd", max: 5 },
] as const;

type PidKey = (typeof PID_PARAMS)[number]["key"];

const STEP = 0.01;

/** 調整できるモータ 1 基。`pid` を持つものだけがここまで来る */
interface Entry {
  robot: string;
  robotLabel: string;
  motor: string;
  motorState: MotorState;
  pid: MotorPid;
}

interface Selection {
  robot: string;
  motor: string;
}

/** 調整対象の 1 値を大きく出す。応答を見ながら詰める作業なので視認性を優先する */
function Readout({ label, value, unit }: { label: string; value: string; unit?: string }) {
  return (
    <div className="flex min-w-0 flex-col items-end">
      <span className="text-[0.8em] text-base-content/60">{label}</span>
      <span className="font-mono text-[1.5em] leading-tight tabular-nums">
        {value}
        {unit ? <span className="text-[0.7em] text-base-content/60">{unit}</span> : null}
      </span>
    </div>
  );
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
  // クランプ後に STEP 単位の浮動小数誤差を丸める。
  const clamp = (val: number) => {
    if (!Number.isFinite(val)) return 0;
    const next = Math.min(max, Math.max(0, val));
    return Math.round(next / STEP) * STEP;
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
        step={STEP}
        value={display(value)}
        onChange={(e) => onChange(clamp(Number(e.target.value)))}
      />
      <input
        type="range"
        className="range min-w-0 flex-1 text-info [--range-thumb:var(--color-info)] range-xs"
        aria-label={`${label} スライダー`}
        min={0}
        max={max}
        step={STEP}
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
 * PID 調整タブ。マスタ・ディテール構成。
 *
 * 以前は両機の全モータを縦に展開しており、1 基を触るだけでもスクロールが要り、
 * 「今どのモータを見ているのか」が視界から外れていた。調整は 1 基ずつ応答を見ながら
 * 詰める作業なので、左で対象を選び、右をその 1 基に明け渡す形にする。
 *
 * **表示する値の出どころはサーバーの `motors[].pid` ただ 1 つ。** 以前はここを持たず
 * 0 で初期化しており、開いた直後の `0.00` をそのまま送ると全ゲインが 0 になって
 * 位置制御ループが無効化された。操縦者には config を読む以外に元の値へ戻す術が無い。
 */
export function MotorTuning() {
  const states = useRobotStates();
  const { matchState, connected, eStopActive } = useRobotStatus();
  const { send } = useRobotCommands();
  const [values, setValues] = useState<Record<string, Partial<Record<PidKey, number>>>>({});
  const [selected, setSelected] = useState<Selection | null>(null);

  // 届いているモータの総数。「まだ何も来ていない」と「来ているが調整対象が無い」は
  // 操縦者の次の行動が違う (待つ / この画面では触れないと理解する)
  const motorCount = ROBOTS.reduce(
    (n, { key }) => n + Object.keys(states[key]?.motors ?? {}).length,
    0,
  );

  // PC 側 PID を持つモータだけが調整対象。判定はサーバーの `pid` の有無だけで行い、
  // ドライバ種別を UI へ書き写さない。欠けた配信は「調整不可」へ倒す
  // (調整できる側へ倒すと、値を持たないまま送る = 0 上書きの経路が戻る)
  const entries: Entry[] = ROBOTS.flatMap(({ key, label }) =>
    Object.entries(states[key]?.motors ?? {}).flatMap(([motor, motorState]) => {
      const pid = motorState.pid ?? null;
      return pid === null ? [] : [{ robot: key, robotLabel: label, motor, motorState, pid }];
    }),
  );

  // 初回描画時にはまだモータが届いていない。届いた時点で先頭を自動選択する
  const active =
    entries.find((e) => selected && e.robot === selected.robot && e.motor === selected.motor) ??
    entries[0];

  const editKey = (entry: Entry) => `${entry.robot}/${entry.motor}`;

  // 未編集の項目はサーバーが配っている現在値をそのまま出す。ここを 0 で埋めると
  // 画面の表示と機体の実際のゲインが食い違う
  const getValue = (entry: Entry, param: PidKey) =>
    values[editKey(entry)]?.[param] ?? entry.pid[param];

  const setValue = (entry: Entry, param: PidKey, val: number) => {
    const key = editKey(entry);
    setValues((prev) => ({ ...prev, [key]: { ...prev[key], [param]: val } }));
  };

  // 3 項目を個別に送ると PID が中途半端に混ざった状態が一瞬できる。まとめて送る
  const sendAll = (entry: Entry) => {
    for (const { key } of PID_PARAMS) {
      send({ type: "set_param", motor: entry.motor, key, value: getValue(entry, key) });
    }
  };

  // 試合中の set_param はサーバーが拒否する (走行中の位置制御ループの特性が変わり、
  // 同期グループ全体に適用されるため直結した左右軸が負荷下で同時に別特性になる)。
  // 緊急停止中も同じく拒否される (`lib/commands.py` の allowed_during_e_stop=False)。
  // 判定の正はサーバーで、ここは押す前に理由を出すだけ。拒否トーストで気付くのでは遅い
  const blockedReason = !connected
    ? "切断中のため送信できません"
    : eStopActive
      ? "緊急停止中はパラメータを変更できません"
      : isDuringMatch(matchState.phase)
        ? "試合中はパラメータを変更できません"
        : null;

  if (entries.length === 0) {
    return (
      <Page className="flex flex-col items-center justify-center">
        <Panel legend="PID TUNING" className="flex-none">
          {motorCount === 0 ? (
            <p className="text-base-content/70">モータ情報なし — 接続待機中...</p>
          ) : (
            <div className="flex flex-col gap-1">
              <p className="text-base-content/70">調整対象のモータがありません</p>
              <p className="text-[0.9em] text-base-content/60">{NO_PC_SIDE_PID_NOTE}</p>
            </div>
          )}
        </Panel>
      </Page>
    );
  }

  return (
    <Page className="grid grid-cols-[minmax(13rem,18rem)_minmax(0,1fr)]">
      <Panel legend="モータ" bodyClassName="p-0">
        <div className="scroll flex min-h-0 flex-1 flex-col">
          {ROBOTS.map(({ key, label }) => {
            const group = entries.filter((e) => e.robot === key);
            if (group.length === 0) return null;
            return (
              <div key={key} className="flex flex-col">
                <div className="sticky top-0 border-b border-base-300 bg-base-200 px-2 py-[0.15rem] text-[0.85em] text-base-content/70">
                  {label}
                </div>
                {group.map((entry) => {
                  const isActive = active?.robot === key && active?.motor === entry.motor;
                  return (
                    <button
                      key={entry.motor}
                      type="button"
                      onClick={() => setSelected({ robot: key, motor: entry.motor })}
                      aria-current={isActive ? "true" : undefined}
                      className={cx(
                        "flex cursor-pointer items-center gap-2 border-l-2 border-transparent px-2 py-[0.3rem] text-left hover:bg-base-200",
                        isActive && "border-l-info bg-base-200 font-medium",
                      )}
                    >
                      <span className="min-w-0 flex-1 truncate">{entry.motor}</span>
                      <span className="shrink-0 font-mono text-[0.85em] text-base-content/60 tabular-nums">
                        {entry.motorState.temp.toFixed(0)}℃
                      </span>
                    </button>
                  );
                })}
              </div>
            );
          })}
        </div>
        {/* 一覧に出ていないモータの説明。絞り込みの理由が画面から読めないと、
            操縦者は「表示されないモータが壊れている」と読む */}
        <p className="border-t border-base-300 px-2 py-1.5 text-[0.8em] leading-snug text-base-content/60">
          {NO_PC_SIDE_PID_NOTE}
        </p>
      </Panel>

      {active ? (
        <MotorDetail
          key={editKey(active)}
          entry={active}
          getValue={getValue}
          setValue={setValue}
          onSend={() => sendAll(active)}
          blockedReason={blockedReason}
        />
      ) : null}
    </Page>
  );
}

const NO_PC_SIDE_PID_NOTE =
  "EDULITE 05 と自作モータドライバはドライバ側で制御しており、PC からゲインを変更できません。";

function MotorDetail({
  entry,
  getValue,
  setValue,
  onSend,
  blockedReason,
}: {
  entry: Entry;
  getValue: (entry: Entry, param: PidKey) => number;
  setValue: (entry: Entry, param: PidKey, val: number) => void;
  onSend: () => void;
  /** 送信できない理由。null なら送れる */
  blockedReason: string | null;
}) {
  const { motor, motorState, pid } = entry;
  // 左右直結ペアはサーバーがグループ全員へ同じ値を入れる (片側だけ別特性にすると
  // 押し合って機構が壊れる)。適用先はサーバーが配る `applies_to` をそのまま描く
  const paired = pid.applies_to.length > 1;

  return (
    <Panel
      legend={`${entry.robotLabel} / ${motor}`}
      actions={
        <StatusBadge tone={motorTempTone(motorState.temp)}>
          {motorState.temp.toFixed(1)}℃
        </StatusBadge>
      }
    >
      {/* 応答を見ながら詰めるので、選択中 1 基の現在値は大きく出す */}
      <div className="flex shrink-0 justify-between gap-4 border-b border-base-300 pb-2">
        <Readout label="POS" value={motorState.pos.toFixed(1)} />
        <Readout label="VEL" value={motorState.vel.toFixed(1)} />
        <Readout label="TORQUE" value={motorState.torque.toFixed(2)} />
        <Readout label="TEMP" value={motorState.temp.toFixed(1)} unit="℃" />
      </div>

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
