import type { HealthSnapshot, MotorState, SafetyState } from "@/lib/protocol";
import { TEMP_DANGER, TEMP_WARNING } from "@/lib/robots";
import type { Tone } from "@/lib/tone";

export interface HealthVerdict {
  tone: Tone;
  label: string;
  /** 判定の理由をラベルに収められないとき (サーバーの判定不能など) の補足 */
  detail?: string;
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
 * ラッチ中の軸が分からないと操縦者は復旧手順を選べず、200Hz の位置制御ループ・
 * 50Hz の同期監視・20Hz の目標値再送が死んだことは配信を読まない限り誰も気付けない
 * (WS は繋がったままでモータ状態も届き続けるため、画面は正常に見える)。
 *
 * 集約値 (`*_running`) と内訳 (`position_loops` 等) は同じ事実の 2 つの見え方なので、
 * 1 タスク種別につき 1 件へ畳む。内訳から対象を挙げられるときはそれを、
 * 挙げられないときだけ「全〜」を detail に置く。
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

  // 緊急停止は解除されているのに励磁が戻っていない。指令は 20Hz で飛び続け、
  // フィードバックもヘルスも正常なので、ここで言わないと誰も気付けない
  if (safety.unenergized_motors.length > 0) {
    issues.push({
      label: "無励磁のまま",
      detail: safety.unenergized_motors.join(", "),
      hint: "指令は届いていますが励磁されていません。緊急停止をもう一度押して解除し直してください (直らなければドライバの電源と CAN 配線を確認)",
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

  // 20Hz の再送が止まると 500ms 後にファーム側のコマンドウォッチドッグが働き、
  // generic アクチュエータ (グリッパ・コンベア・壁) が一斉に停止する。
  // 位置制御ループ・同期監視の停止と同格の異常として扱う
  const deadRefreshers = safety.target_refreshers
    .filter((r) => !r.running)
    .flatMap((r) => r.motors);
  if (deadRefreshers.length > 0 || !safety.refreshers_running) {
    issues.push({
      label: "目標値再送停止",
      detail: deadRefreshers.length > 0 ? deadRefreshers.join(", ") : "全モータ",
      hint: "20Hz の再送が止まっています。500ms 後にファーム側ウォッチドッグでグリッパ・コンベア・壁が停止します",
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
 *
 * **サーバーの `overall` より楽観的な結論を出してはならない。** サーバーは健全性を
 * 計算できなかったときに overall=down・内訳空で「判定不能」を配信する。内訳だけを見て
 * 「異常なし」を返すと、そのフェイルセーフが画面上で消える。
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

  // 内訳から理由を挙げられないのに overall が down = サーバーが健全性を判定できていない
  // (`lib/server.py` の `_health_unknown`)。ここで success を返すと、判定不能を
  // DOWN へ倒すサーバーのフェイルセーフを UI 側が打ち消してしまう。
  // 「異常の有無が分からない」は安全側では異常であって正常ではない
  if (health.overall === "down") {
    return {
      tone: "error",
      label: "健全性 判定不能",
      detail: health.detail ?? "サーバーがヘルスを判定できていません",
    };
  }

  const degraded = health.buses.filter((b) => b.state !== "ok").length;
  const badMotors = health.motors.filter((m) => m.state !== "ok").length;
  const warnCount = degraded + badMotors + countHotMotors(motors);
  if (warnCount > 0) return { tone: "warning", label: `要確認 ${warnCount} 件` };

  // 内訳に異常が無くてもサーバーの総合判定より楽観的になってはならない
  if (health.overall !== "ok") {
    return {
      tone: "warning",
      label: "要確認 (サーバー判定 degraded)",
      detail: health.detail ?? undefined,
    };
  }

  return { tone: "success", label: "異常なし" };
}
