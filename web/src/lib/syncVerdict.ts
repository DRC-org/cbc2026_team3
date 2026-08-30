import type { ManualAxis } from "@/lib/protocol";
import type { Tone } from "@/lib/tone";

/**
 * 左右直結ペアのずれを、どう見せるかの判定。
 *
 * **判定はここにしか置かない。** 同じ事実に対する色と文言が 2 箇所にあると、
 * 「行は警告なのに軸の見出しは平常」のような食い違いが生まれる
 * (`lib/healthVerdict.ts` と同じ原則)。
 *
 * **しきい値のフォールバック値を持たない。** 正は config の `sync_tolerance` だけで、
 * `state.manual` に載って届く。届いていない間は `neutral` に倒し、適当な既定値で
 * 「正常」とも「警告」とも言わない —— 機構を壊す判断の根拠を UI が捏造しない。
 */
export interface SyncVerdict {
  tone: Tone;
  /** 許容差に対する比率 [0, 1+]。しきい値未配信なら null (バーを描かない) */
  ratio: number | null;
  /** 平常時に自分から主張しないための旗。false ならチップを出さず数値だけ出す */
  alert: boolean;
}

/**
 * 許容差のどれだけを使っていれば警告に入るか。
 *
 * 超過の一歩手前を `warning` にするのは、超過した時点では既に機体が止まっている
 * (50Hz の `SyncMonitor` が全体緊急停止を掛ける) ためで、操縦者が手を打てるのは
 * それより前の区間しかない。
 */
export const SYNC_WARN_RATIO = 0.6;

/**
 * 偏差の見せ方を決める。
 *
 * `deviation` の `0` は **正常な測定値** (完全に揃っている) であって欠落ではない。
 * falsy 判定で捨てると、最も健全な状態だけが「測れていない」と表示される。
 */
export function evaluateSync(axis: Pick<ManualAxis, "deviation" | "sync_tolerance">): SyncVerdict {
  const { deviation, sync_tolerance: tolerance } = axis;

  // ずれようのない軸 (単独モータ) と測れない軸。どちらも語ることが無い
  if (typeof deviation !== "number" || !Number.isFinite(deviation)) {
    return { tone: "neutral", ratio: null, alert: false };
  }
  // 偏差は読めているがしきい値が届いていない。数値は出せるが判定はしない
  if (typeof tolerance !== "number" || !Number.isFinite(tolerance) || tolerance <= 0) {
    return { tone: "neutral", ratio: null, alert: false };
  }

  const ratio = Math.abs(deviation) / tolerance;
  if (ratio > 1) return { tone: "error", ratio, alert: true };
  if (ratio >= SYNC_WARN_RATIO) return { tone: "warning", ratio, alert: true };
  return { tone: "success", ratio, alert: false };
}
