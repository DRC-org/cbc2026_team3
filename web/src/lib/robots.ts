// switchMeasure: 操縦者画面の手動操縦の区画に作動点測定のボタンを出す機体
export const ROBOTS = [
  { key: "main_hand", label: "Main Hand", switchMeasure: false },
  { key: "sub_hand", label: "Sub Hand", switchMeasure: true },
] as const;

export function hasSwitchMeasure(robotKey: string): boolean {
  return ROBOTS.some((robot) => robot.key === robotKey && robot.switchMeasure);
}
