/**
 * 指令値 1 つの表示文字列。
 *
 * **語を決めるのは `command_mode` だけ。** モータ名や基板の種類から推測すると、
 * ドライバ種別を UI へ書き写すことになる。
 *
 * `on_off` は電磁弁の開閉指令で、基板は 0 か非 0 かしか見ない。0.0 / 1.0 と数字で
 * 出すと duty と見分けが付かないので `ON` / `OFF` と書く。**位置定数 yaml の
 * `unit` も `on_off` という文字列なので、単位として添えてはならない** ——
 * 「1.00 on_off」は単位でも状態でもない表示にしかならない。
 *
 * 桁数は呼び出し側が決める。診断表 (`MotorStatus`) は幅 300px の列に収めるため
 * 詰めるが、手動操縦 (`ManualAxisRow`) はジョグの刻みが 0.5 まであるので 2 桁が要る。
 *
 * ここを 2 箇所に書くと、片方だけ語を変えたときに同じ弁が画面の場所によって
 * `ON` と `1.00` の 2 通りに見える。
 */
export function commandValueText(
  value: number,
  mode: string | null | undefined,
  fractionDigits: number,
): string {
  if (mode === "on_off") return value === 0 ? "OFF" : "ON";
  return value.toFixed(fractionDigits);
}

/** `on_off` の指令に単位は無い (`unit` に入っているのは単位ではなく指令の種類) */
export function hasUnit(mode: string | null | undefined): boolean {
  return mode !== "on_off";
}
