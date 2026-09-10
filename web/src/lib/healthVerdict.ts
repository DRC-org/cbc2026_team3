import { MALFORMED, healthShapeErrors, safetyShapeErrors } from "@/lib/protocol";
import type {
  BusHealth,
  HealthSnapshot,
  Malformed,
  MotorHealth,
  SafetyState,
  ServerInfo,
} from "@/lib/protocol";
import type { Tone } from "@/lib/tone";

export type SafetyPayload = SafetyState | Malformed;

export type HealthPayload = HealthSnapshot | Malformed;

export function readableHealth(health: HealthPayload | undefined): HealthSnapshot | undefined {
  return health === undefined || health === MALFORMED ? undefined : health;
}

export interface HealthVerdict {
  tone: Tone;
  label: string;
  detail?: string;
}

export type SafetyIssueKind =
  | "unknown"
  | "sync_violation"
  | "unresponsive"
  | "unenergized"
  | "loops_stopped"
  | "monitors_stopped"
  | "limit_monitors_stopped"
  | "refreshers_stopped";

export interface SafetyIssue {
  kind: SafetyIssueKind;
  label: string;
  detail: string;
  hint: string;
}

export function isReenergizePending(safety: SafetyPayload | undefined): boolean {
  if (safety === undefined || safety === MALFORMED) return false;
  return safetyShapeErrors(safety).length === 0 && safety.reenergizing;
}

export interface TempThresholds {
  warning: number;
  critical: number;
}

export function tempThresholdsOf(serverInfo: ServerInfo | undefined): TempThresholds | null {
  const warning = serverInfo?.temp_warning_c;
  const critical = serverInfo?.temp_critical_c;
  if (typeof warning !== "number" || typeof critical !== "number") return null;
  return { warning, critical };
}

export function summarizeMotors(healthMotors: MotorHealth[] | undefined): HealthVerdict {
  if (!healthMotors || healthMotors.length === 0) {
    return { tone: "neutral", label: "ヘルス未取得" };
  }

  const anomalies = healthMotors.filter((m) => m.state !== "ok");
  if (anomalies.length === 0) return { tone: "success", label: "All operational" };

  const tone: Tone = anomalies.some((m) => m.state === "fault") ? "error" : "warning";
  return { tone, label: `異常 ${anomalies.length} 件` };
}

export function motorTempTone(
  temp: number | null | undefined,
  thresholds: TempThresholds | null,
): Tone {
  if (temp === null || temp === undefined) return "neutral";
  if (!thresholds) return "neutral";
  if (temp >= thresholds.critical) return "error";
  if (temp >= thresholds.warning) return "warning";
  return "success";
}

export function workpieceRiskBuses(health: HealthPayload | undefined): BusHealth[] {
  const readable = readableHealth(health);
  if (!readable) return [];
  return readable.buses.filter((b) => b.may_affect_workpiece && b.rx_down_episodes > 0);
}

export function firmwareUnconfirmedMotors(safety: SafetyPayload | undefined): string[] {
  if (!safety || safety === MALFORMED) return [];
  if (safetyShapeErrors(safety).length > 0) return [];
  return safety.firmware_unconfirmed_motors;
}

export function failedTasks(safety: SafetyPayload | undefined): string[] {
  if (!safety || safety === MALFORMED) return [];
  if (safetyShapeErrors(safety).length > 0) return [];
  return safety.failed_tasks;
}

function safetyUnknown(detail: string): SafetyIssue {
  return {
    kind: "unknown",
    label: "安全機構 判定不能",
    detail,
    hint: "安全機構の配信を読めていません。同期ずれラッチも保護ループの停止も検知できない状態です — 機体を動かす前にサーバーのログを確認してください",
  };
}

export function describeSafetyIssues(safety: SafetyPayload | undefined): SafetyIssue[] {
  if (!safety) return [];

  if (safety === MALFORMED) return [safetyUnknown("安全機構の配信全体")];
  const broken = safetyShapeErrors(safety);
  if (broken.length > 0) return [safetyUnknown(broken.join(", "))];

  const issues: SafetyIssue[] = [];

  if (safety.sync_violations.length > 0) {
    issues.push({
      kind: "sync_violation",
      label: "同期ずれラッチ",
      detail: safety.sync_violations.join(", "),
      hint: "機構のずれか左右の原点の食い違いを直してから緊急停止を解除し直してください (解除しただけでは動きません。どちらなのかは緊急停止の理由が出します)",
    });
  }

  if (safety.unresponsive_motors.length > 0) {
    issues.push({
      kind: "unresponsive",
      label: "応答なし",
      detail: safety.unresponsive_motors.join(", "),
      hint: "フィードバックが 1 通も届いていません。ドライバの電源と CAN 配線を確認してください (再励磁を押しても直りません)",
    });
  }

  if (safety.unenergized_motors.length > 0) {
    issues.push({
      kind: "unenergized",
      label: "無励磁のまま",
      detail: safety.unenergized_motors.join(", "),
      hint: "励磁されていません。操縦者画面の「再励磁」ボタンを押してください (直らなければ緊急停止をもう一度押して解除し直すか、ドライバの電源と CAN 配線を確認)",
    });
  }

  const deadLoops = safety.position_loops.filter((l) => !l.running).map((l) => l.bus);
  if (deadLoops.length > 0 || !safety.loops_running) {
    issues.push({
      kind: "loops_stopped",
      label: "位置制御ループ停止",
      detail: deadLoops.length > 0 ? deadLoops.join(", ") : "全バス",
      hint: "200Hz の位置制御が動いていません。M3508 は指令を失っています",
    });
  }

  const deadMonitors = safety.sync_monitors.filter((m) => !m.running).flatMap((m) => m.axes);
  if (deadMonitors.length > 0 || !safety.monitors_running) {
    issues.push({
      kind: "monitors_stopped",
      label: "同期監視停止",
      detail: deadMonitors.length > 0 ? deadMonitors.join(", ") : "全軸",
      hint: "左右のずれを誰も見ていません。ペア軸の破損を検知できません",
    });
  }

  const deadLimitMonitors = safety.limit_monitors.filter((m) => !m.running).flatMap((m) => m.axes);
  if (deadLimitMonitors.length > 0 || !safety.limit_monitors_running) {
    issues.push({
      kind: "limit_monitors_stopped",
      label: "可動端監視停止",
      detail: deadLimitMonitors.length > 0 ? deadLimitMonitors.join(", ") : "全軸",
      hint: "移動中に端のスイッチを誰も見ていません。遠い目標を 1 回指令すると可動端を踏み越えます",
    });
  }

  const deadRefreshers = safety.target_refreshers
    .filter((r) => !r.running)
    .flatMap((r) => r.motors);
  if (deadRefreshers.length > 0 || !safety.refreshers_running) {
    issues.push({
      kind: "refreshers_stopped",
      label: "目標値再送停止",
      detail: deadRefreshers.length > 0 ? deadRefreshers.join(", ") : "全モータ",
      hint: "20Hz の再送が止まっています。500ms 後にファーム側ウォッチドッグでグリッパ・コンベア・壁が停止します",
    });
  }

  return issues;
}

export function evaluateHealth(
  health: HealthPayload | undefined,
  safety: SafetyPayload | undefined,
  connected: boolean,
): HealthVerdict {
  if (!connected) {
    return {
      tone: "neutral",
      label: "通信断のため判定不能",
      detail: "サーバーと切断しています。表示は切断時点の値で、今の機体の状態ではありません",
    };
  }

  const [safetyIssue] = describeSafetyIssues(safety);
  if (safetyIssue) {
    return { tone: "error", label: `${safetyIssue.label} ${safetyIssue.detail}` };
  }

  if (!health) return { tone: "neutral", label: "ヘルス未取得" };

  if (health === MALFORMED) {
    return {
      tone: "error",
      label: "健全性 判定不能",
      detail: "ヘルスの配信を読めていません。CAN もモータも異常を検知できない状態です",
    };
  }
  const brokenHealth = healthShapeErrors(health);
  if (brokenHealth.length > 0) {
    return {
      tone: "error",
      label: "健全性 判定不能",
      detail: `ヘルスの配信を読めていません (${brokenHealth.join(", ")})`,
    };
  }

  const downBuses = health.buses.filter((b) => b.state === "down");
  if (downBuses.length > 0) {
    return {
      tone: "error",
      label: `CAN 停止 ${downBuses[0].name}`,
      detail: health.detail ?? undefined,
    };
  }

  const faultMotors = health.motors.filter((m) => m.state === "fault");
  if (faultMotors.length > 0) {
    return {
      tone: "error",
      label: `モータ異常 ${faultMotors.length} 件 (${faultMotors[0].name})`,
      detail: health.detail ?? undefined,
    };
  }

  if (health.overall === "down") {
    return {
      tone: "error",
      label: "健全性 判定不能",
      detail: health.detail ?? "サーバーがヘルスを判定できていません",
    };
  }

  const degraded = health.buses.filter((b) => b.state !== "ok").length;
  const badMotors = health.motors.filter((m) => m.state !== "ok").length;
  const warnCount = degraded + badMotors;
  if (warnCount > 0) return { tone: "warning", label: `要確認 ${warnCount} 件` };

  if (health.overall !== "ok") {
    return {
      tone: "warning",
      label: "要確認 (サーバー判定 degraded)",
      detail: health.detail ?? undefined,
    };
  }

  return { tone: "success", label: "異常なし" };
}
