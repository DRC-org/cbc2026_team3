import type { MotorPid, MotorState } from "@/lib/protocol";

/**
 * PID 調整画面の語彙。**ページと部品が共有するので、どちらの component ファイルにも
 * 置かない** (表示部品が別の表示部品のユーティリティ置き場になると、片方を消した
 * ときに巻き添えで壊れる)。
 */

/**
 * 調整対象の 3 項目と、スライダーの作業レンジ。
 *
 * `max` は作業レンジであってクランプではない。config の値がこれを超えていたら
 * **レンジ側を広げる** —— 上限で切ると、表示された値と機体の実際のゲインが食い違う。
 */
export const PID_PARAMS = [
  { key: "kp", label: "Kp", max: 10 },
  { key: "ki", label: "Ki", max: 5 },
  { key: "kd", label: "Kd", max: 5 },
] as const;

export type PidKey = (typeof PID_PARAMS)[number]["key"];

/** 数値入力・スライダーの刻み */
export const PID_STEP = 0.01;

/** 調整できるモータ 1 基。`pid` を持つものだけがここまで来る */
export interface TuningEntry {
  robot: string;
  robotLabel: string;
  motor: string;
  motorState: MotorState;
  pid: MotorPid;
}

/** `tuningCaptures` と編集中の値のキー。モータ名は横断で一意だが画面はロボットで分ける */
export function entryKey(entry: TuningEntry): string {
  return `${entry.robot}/${entry.motor}`;
}

/**
 * 一覧に出ていないモータの説明。絞り込みの理由が画面から読めないと、
 * 操縦者は「表示されないモータが壊れている」と読む。
 */
export const NO_PC_SIDE_PID_NOTE =
  "EDULITE 05 と自作モータドライバはドライバ側で制御しており、PC からゲインを変更できません。";

/**
 * 目標との差。目標を持たないモータ・停止中・**位置を測れないモータ**は null。
 *
 * **0 を返してはならない。** 「目標に完璧に追従している」と「そもそも目標が無い」が
 * 同じ表示になり、停止中の機体を追従できていると読む経路ができる。位置を測れない
 * モータ (`pos === null`) を 0 として引き算するのも同じ罠 —— 偏差そのものが
 * 目標値の符号違いに化けたうえで、測っていないことが画面から消える。
 */
export function deviationOf(motor: MotorState): number | null {
  if (motor.target === null || motor.pos === null) return null;
  return motor.target - motor.pos;
}
