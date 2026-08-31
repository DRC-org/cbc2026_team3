/** daisyUI のセマンティックカラーに対応する状態キー。 */
export type Tone = "success" | "warning" | "error" | "info" | "neutral";

export const TONE_TEXT_CLASS: Record<Tone, string> = {
  success: "text-success",
  warning: "text-warning",
  error: "text-error",
  info: "text-info",
  neutral: "text-base-content/70",
};

export const TONE_PROGRESS_CLASS: Record<Tone, string> = {
  success: "progress-success",
  warning: "progress-warning",
  error: "progress-error",
  info: "progress-info",
  neutral: "",
};

/**
 * 状態チップの配色。daisyUI の `badge` は `badge-soft` と色修飾子が揃って
 * 初めて淡色地になる。Tailwind はソースに現れた文字列ぶんしか CSS を出さないので、
 * 修飾子を分けて組み立てず必ず 3 つ揃った文字列としてここに書く。
 */
export const TONE_BADGE_CLASS: Record<Tone, string> = {
  success: "badge badge-soft badge-success",
  warning: "badge badge-soft badge-warning",
  error: "badge badge-soft badge-error",
  info: "badge badge-soft badge-info",
  neutral: "badge badge-soft badge-neutral",
};

/** 状態インジケータ（正方形の LED）。`status` と色修飾子は対で書く。 */
export const TONE_STATUS_CLASS: Record<Tone, string> = {
  success: "status status-success",
  warning: "status status-warning",
  error: "status status-error",
  info: "status status-info",
  neutral: "status",
};

/**
 * 左端のアクセントバー。
 * Tailwind はソースに現れた文字列ぶんしか CSS を出さないので、`border-` を
 * 実行時に `border-l-` へ置換するような組み立て方をしてはならない（CSS ごと消える）。
 */
export const TONE_BORDER_L_CLASS: Record<Tone, string> = {
  success: "border-l-success",
  warning: "border-l-warning",
  error: "border-l-error",
  info: "border-l-info",
  neutral: "border-l-base-300",
};

/**
 * 通知バナー (トースト) の配色。daisyUI の `alert` は色修飾子と対で書かないと
 * 配色ルールごと CSS から消える。ここに置くのは `daisyPairs.test.tsx` の
 * 検査に載せるためで、トースト側にローカル定義を持たせてはならない。
 */
export const TONE_ALERT_CLASS: Record<Tone, string> = {
  success: "alert alert-success",
  warning: "alert alert-warning",
  error: "alert alert-error",
  info: "alert alert-info",
  // 既定の alert (色無し)。daisyUI に alert-neutral は無い
  neutral: "alert",
};
