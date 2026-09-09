const LABELS: Record<string, string> = {
  main_hand: "メインハンド",
  sub_hand: "サブハンド",
};

export function robotLabel(robot: string): string {
  return LABELS[robot] ?? robot;
}
