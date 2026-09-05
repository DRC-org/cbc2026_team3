import { StatusBadge } from "@/components/ui/StatusBadge";
import { MALFORMED } from "@/lib/protocol";
import type { Malformed, SensorState } from "@/lib/protocol";
import type { Tone } from "@/lib/tone";

/** `state.sensors` として画面まで来うる形。未配信は undefined */
export type SensorPayload = Record<string, SensorState> | Malformed;

interface SensorSummaryProps {
  /** 受信境界を通っていない props 経路が残るので、読めなかった配信も受ける */
  sensors: SensorPayload | undefined;
}

interface SensorVerdict {
  tone: Tone;
  label: string;
}

/**
 * センサ 1 個の見え方。
 *
 * **接触に警告色を使わない。** 原点合わせは「触れさせる」操作なので、接触は
 * 平常の情報であって異常ではない (サーバー側もドライバの `is_fault()` に
 * 入れていない)。異常なのは途絶の方だけ。
 *
 * 接触を `info`、開放を `neutral` で描き分けるのは、指差喚呼
 * (`config/checklist.yaml` の `origin_sensor_react`) が「1 本ずつ触れて反応を見る」
 * 操作だから —— 押した瞬間にどちらの状態かが一目で切り替わる必要がある。
 */
function verdictOf(sensor: SensorState): SensorVerdict {
  // 途絶を先に見る。触れていないのか届いていないのかを取り違えると、
  // 零点確定は探索距離いっぱいまで機構を押し込んでから失敗する
  if (sensor.stale) return { tone: "warning", label: "STALE" };
  // 接触を報告する手段が無いドライバ。false (触れていない) と混ぜてはならない
  if (sensor.active === null) return { tone: "neutral", label: "—" };
  return sensor.active ? { tone: "info", label: "接触" } : { tone: "neutral", label: "開放" };
}

/**
 * 自作基板のセンサ入力 (原点スイッチ) の一覧。
 *
 * **モータ一覧には混ぜない。** サーバーも `sensors:` を `motors:` と別のセクションに
 * 持っており (動作確認・目標値再送・モータ一覧に「常に 0 のモータ」を並べないため)、
 * ここで混ぜるとその判断が画面側から消える。
 *
 * ここが無いと、`origin_sensor_react` の指差喚呼は `candump` を打たない限り
 * 確かめられない。さらに**未配線・極性違いのセンサは STALE にすらならない** ——
 * 基板は配線の有無に関わらず FEEDBACK を送り、`INPUT_PULLUP` の負論理で
 * 「接触なし」を報告し続けるので、触れてみる以外に検出手段が無い。
 *
 * 全体の健全性判定 (`evaluateHealth` / `summarizeMotors`) はここでは行わない。
 * サーバーの `CANManager.health` がセンサも同じ鮮度でヘルスへ載せているので、
 * 途絶は既にその件数へ数えられている (判定を 2 箇所に置かない)。
 */
export function SensorSummary({ sensors }: SensorSummaryProps) {
  // 未配信 (センサを配らない版のサーバー) と、センサを 1 本も持たない構成は
  // どちらも「出すものが無い」。1px も占めないのが正しい
  if (sensors === undefined) return null;

  if (sensors === MALFORMED) {
    return (
      <div className="flex shrink-0 flex-col gap-1 border-l-[0.25rem] border-l-error bg-error/5 px-2 py-1">
        <span className="font-medium">センサ 判定不能</span>
        <span className="text-[0.85em] text-base-content/70">
          センサの配信を読めていません。原点スイッチが反応しているかを画面から確かめられない状態です
        </span>
      </div>
    );
  }

  const entries = Object.entries(sensors);
  if (entries.length === 0) return null;

  return (
    <div className="flex shrink-0 flex-col gap-1">
      <span className="text-base-content/70">センサ {entries.length} 本</span>
      <div className="flex flex-col [&>*:nth-child(odd)]:bg-base-200">
        {entries.map(([name, sensor]) => {
          const verdict = verdictOf(sensor);
          return (
            <div
              key={name}
              className="flex min-w-0 items-center justify-between gap-2 px-1 py-[0.15rem]"
            >
              <span className="min-w-0 truncate font-medium">{name}</span>
              <StatusBadge tone={verdict.tone}>{verdict.label}</StatusBadge>
            </div>
          );
        })}
      </div>
    </div>
  );
}
