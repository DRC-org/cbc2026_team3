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

export const TONE_BADGE_CLASS: Record<Tone, string> = {
  success: "badge badge-soft badge-success",
  warning: "badge badge-soft badge-warning",
  error: "badge badge-soft badge-error",
  info: "badge badge-soft badge-info",
  neutral: "badge badge-soft badge-neutral",
};

export const TONE_STATUS_CLASS: Record<Tone, string> = {
  success: "status status-success",
  warning: "status status-warning",
  error: "status status-error",
  info: "status status-info",
  neutral: "status",
};

export const TONE_BORDER_L_CLASS: Record<Tone, string> = {
  success: "border-l-success",
  warning: "border-l-warning",
  error: "border-l-error",
  info: "border-l-info",
  neutral: "border-l-base-300",
};

export const TONE_ALERT_CLASS: Record<Tone, string> = {
  success: "alert alert-success",
  warning: "alert alert-warning",
  error: "alert alert-error",
  info: "alert alert-info",
  // daisyUI に alert-neutral は無い
  neutral: "alert",
};
