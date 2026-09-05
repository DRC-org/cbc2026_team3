import type { MotorState } from "@/lib/protocol";

/**
 * テスト用のモータ状態。**見たい 1 項目だけを渡す。**
 *
 * 各テストが `MotorState` のリテラルを書き写していると、サーバーが 1 フィールド
 * 増やすたびに無関係なテストが機械的に赤くなり、そのたびに「型合わせ」として
 * テストを実装へ追従させることになる。組み立てを 1 箇所に集めておけば、
 * 増えたフィールドの既定値もここだけで決まる。
 *
 * 既定は「PC 側 PID を持たないモータが静止している」状態にしてある。
 * `target: null` を既定にするのは、`0` だと偏差 0 =完璧に追従している、と
 * 読めてしまうため (サーバー側も同じ理由で 0 を配らない)。
 */
export function motorState(overrides: Partial<MotorState> = {}): MotorState {
  return {
    pos: 0,
    vel: 0,
    torque: 0,
    temp: 30,
    pid: null,
    target: null,
    // 既定は「一度も指令していない」。値を入れると、指令値を出すかどうかを
    // 見ていないテストにまで `→` 付きの表示が紛れ込む
    command: null,
    command_mode: null,
    saturated: false,
    ...overrides,
  };
}
