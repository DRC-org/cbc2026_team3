/**
 * 時刻の単位を型名で明示する。
 *
 * サーバー (Python) の `time.time()` は **エポック秒**、JS の `Date` と `Date.now()` は
 * **エポックミリ秒**。両者が同じ `number` だったため、動作確認の実施時刻に秒を
 * `new Date(ms)` へ渡して常に 1970-01-01 を表示していた。指差喚呼で
 * 「アクチュエータ動作確認 完了」にチェックする直前の唯一の判断材料が嘘になる。
 *
 * 以後、受信境界 (`useRobotSocket`) で必ず ms へ正規化し、UI 状態のフィールド名は
 * `...Ms` で終わらせる。ワイヤ形式のフィールド (`started_at` 等) だけが
 * `EpochSeconds` を名乗る。
 */

/** サーバー配信そのままのエポック秒。Date に渡してはならない */
export type EpochSeconds = number;

/** エポックミリ秒。Date / Date.now() と同じ土俵の値 */
export type EpochMs = number;

/** 壁時計表示 (HH:MM:SS)。引数は必ずエポック**ミリ秒** */
export function formatClock(ms: EpochMs | null | undefined): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return "—";
  return new Date(ms).toLocaleTimeString("ja-JP", { hour12: false });
}

/**
 * 経過時間を「n 前」の形にする。**測れていないものはダッシュ**で、0 にしない。
 *
 * フィードバックの鮮度表示に使う。null を `0ms 前` と書くと「今まさに届いた」に
 * 化け、途絶したモータが最も健康に見える。負値も同じ扱いにする (時刻ずれであって
 * 「未来に受信した」ではない)。
 */
export function formatAge(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || Number.isNaN(ms)) return "—";
  if (ms < 0) return "—";
  if (ms < 1000) return `${Math.round(ms)}ms 前`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s 前`;
  if (ms < 3_600_000) return `${Math.floor(ms / 60_000)}m 前`;
  return `${Math.floor(ms / 3_600_000)}h 前`;
}
