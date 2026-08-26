import type { HealthSnapshot, MotorState } from "@/hooks/useRobotSocket";
import { TEMP_WARNING } from "@/lib/robots";
import type { Tone } from "@/lib/tone";

export interface HealthVerdict {
  tone: Tone;
  label: string;
}

/**
 * CAN とモータの状態を 1 つの判定へ畳む。
 *
 * 「異常があるか」は診断カラム (SubsystemStatus) と試合開始の可否表示 (StartGate) の
 * 双方が答える必要がある。判定を 2 箇所に書くと、片方だけ直したときに
 * 「Monitor は READY と言っているのに操縦者の画面は異常と言っている」状態が生まれる。
 */
export function evaluateHealth(
  health: HealthSnapshot | undefined,
  motors: Record<string, MotorState>,
): HealthVerdict {
  if (!health) return { tone: "neutral", label: "ヘルス未取得" };

  const downBuses = health.buses.filter((b) => b.state === "down");
  if (downBuses.length > 0) return { tone: "error", label: `CAN 停止 ${downBuses[0].name}` };

  const faultMotors = health.motors.filter((m) => m.state === "fault");
  if (faultMotors.length > 0) {
    return { tone: "error", label: `モータ異常 ${faultMotors.length} 件 (${faultMotors[0].name})` };
  }

  const degraded = health.buses.filter((b) => b.state !== "ok").length;
  const badMotors = health.motors.filter((m) => m.state !== "ok").length;
  const hot = Object.values(motors).filter((m) => m.temp >= TEMP_WARNING).length;
  const warnCount = degraded + badMotors + hot;
  if (warnCount > 0) return { tone: "warning", label: `要確認 ${warnCount} 件` };

  return { tone: "success", label: "異常なし" };
}
