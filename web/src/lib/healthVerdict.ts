import type { HealthSnapshot, MotorState, SafetyState } from "@/hooks/useRobotSocket";
import { TEMP_DANGER, TEMP_WARNING } from "@/lib/robots";
import type { Tone } from "@/lib/tone";

export interface HealthVerdict {
  tone: Tone;
  label: string;
}

/** 安全機構の異常 1 件。`hint` は操縦者が次に取るべき行動 */
export interface SafetyIssue {
  label: string;
  detail: string;
  hint: string;
}

/** 警告温度に達しているモータの基数。「異常 N 件」の N をここでしか数えない */
export function countHotMotors(motors: Record<string, MotorState>): number {
  return Object.values(motors).filter((m) => m.temp >= TEMP_WARNING).length;
}

/**
 * モータ温度の状態色。
 *
 * 以前 `MotorStatus` と `MotorTuning` に別実装があり、片方は独自の
 * `StatTone`、もう片方は `Tone` を返していた。しきい値の変更が片方にしか
 * 効かない構造だったので、判定はここ 1 箇所に置く。
 */
export function motorTempTone(temp: number | null | undefined): Tone {
  if (temp === null || temp === undefined) return "neutral";
  if (temp >= TEMP_DANGER) return "error";
  if (temp >= TEMP_WARNING) return "warning";
  return "success";
}

/**
 * 安全機構の異常を列挙する。平常時は空配列 (画面に何も足さない)。
 *
 * ラッチ中の軸が分からないと操縦者は復旧手順を選べず、200Hz の位置制御ループと
 * 50Hz の同期監視が死んだことは配信を読まない限り誰も気付けない
 * (WS は繋がったままでモータ状態も届き続けるため、画面は正常に見える)。
 */
export function describeSafetyIssues(safety: SafetyState | undefined): SafetyIssue[] {
  if (!safety) return [];
  const issues: SafetyIssue[] = [];

  if (safety.sync_violations.length > 0) {
    issues.push({
      label: "同期ずれラッチ",
      detail: safety.sync_violations.join(", "),
      hint: "機構を直してから緊急停止を解除し直してください (解除しただけでは動きません)",
    });
  }

  // paused は動作確認中の意図的な停止なので異常に数えない
  const deadLoops = safety.position_loops.filter((l) => !l.running).map((l) => l.bus);
  if (deadLoops.length > 0 || !safety.loops_running) {
    issues.push({
      label: "位置制御ループ停止",
      detail: deadLoops.length > 0 ? deadLoops.join(", ") : "全バス",
      hint: "200Hz の位置制御が動いていません。M3508 は指令を失っています",
    });
  }

  const deadMonitors = safety.sync_monitors.filter((m) => !m.running).flatMap((m) => m.axes);
  if (deadMonitors.length > 0 || !safety.monitors_running) {
    issues.push({
      label: "同期監視停止",
      detail: deadMonitors.length > 0 ? deadMonitors.join(", ") : "全軸",
      hint: "左右のずれを誰も見ていません。ペア軸の破損を検知できません",
    });
  }

  return issues;
}

/**
 * CAN・モータ・安全機構の状態を 1 つの判定へ畳む。
 *
 * 「異常があるか」は診断カラム (SubsystemStatus)、試合開始の可否表示 (StartGate)、
 * タブの LED (TabBar) が答える必要がある。判定を複数箇所に書くと、片方だけ直したときに
 * 「Monitor は READY と言っているのに操縦者の画面は異常と言っている」状態が生まれる
 * (実際にタブだけが degraded を error 扱いしていた)。
 *
 * 安全機構をバス・モータより先に見るのは、復旧操作が別物だから。
 * ラッチ中の軸は緊急停止を解除しても動かず、CAN やモータの表示を見ても理由が分からない。
 */
export function evaluateHealth(
  health: HealthSnapshot | undefined,
  motors: Record<string, MotorState>,
  safety?: SafetyState,
): HealthVerdict {
  // 判定と詳細表示を同じ列挙から作る。チップは「種別 + 対象」、詳細行は復旧手順を担う
  const [safetyIssue] = describeSafetyIssues(safety);
  if (safetyIssue) {
    return { tone: "error", label: `${safetyIssue.label} ${safetyIssue.detail}` };
  }

  if (!health) return { tone: "neutral", label: "ヘルス未取得" };

  const downBuses = health.buses.filter((b) => b.state === "down");
  if (downBuses.length > 0) return { tone: "error", label: `CAN 停止 ${downBuses[0].name}` };

  const faultMotors = health.motors.filter((m) => m.state === "fault");
  if (faultMotors.length > 0) {
    return { tone: "error", label: `モータ異常 ${faultMotors.length} 件 (${faultMotors[0].name})` };
  }

  const degraded = health.buses.filter((b) => b.state !== "ok").length;
  const badMotors = health.motors.filter((m) => m.state !== "ok").length;
  const warnCount = degraded + badMotors + countHotMotors(motors);
  if (warnCount > 0) return { tone: "warning", label: `要確認 ${warnCount} 件` };

  return { tone: "success", label: "異常なし" };
}
