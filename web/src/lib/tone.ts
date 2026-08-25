/** daisyUI のセマンティックカラーに対応する状態キー。 */
export type Tone = "success" | "warning" | "error" | "info" | "neutral";

export const TONE_TEXT_CLASS: Record<Tone, string> = {
  success: "text-success",
  warning: "text-warning",
  error: "text-error",
  info: "text-info",
  neutral: "text-fg-dim",
};

export const TONE_PROGRESS_CLASS: Record<Tone, string> = {
  success: "progress-success",
  warning: "progress-warning",
  error: "progress-error",
  info: "progress-info",
  neutral: "",
};
