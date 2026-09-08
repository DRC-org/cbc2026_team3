export function commandValueText(
  value: number,
  mode: string | null | undefined,
  fractionDigits: number,
): string {
  if (mode === "on_off") return value === 0 ? "OFF" : "ON";
  return value.toFixed(fractionDigits);
}

// 位置定数 yaml の `unit` は on_off 軸でも "on_off" という文字列なので単位に使えない
export function hasUnit(mode: string | null | undefined): boolean {
  return mode !== "on_off";
}
