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

/** `state.safety` として画面まで来うる形。未配信は undefined */
export type SafetyPayload = SafetyState | Malformed;

/** `state.health` として画面まで来うる形。未配信は undefined */
export type HealthPayload = HealthSnapshot | Malformed;

/**
 * 描画にそのまま使えるヘルスだけを取り出す。読めなかった配信 (`MALFORMED`) は undefined。
 *
 * 内訳を並べる部品 (`HealthIndicator` / `MotorSummary`) は「読めなかった」を表現する
 * 手段を持たないので、渡す前にここで落とす。**判定側 (`evaluateHealth`) は落とさない**
 * —— あちらは MALFORMED を異常として出す役。
 */
export function readableHealth(health: HealthPayload | undefined): HealthSnapshot | undefined {
  return health === undefined || health === MALFORMED ? undefined : health;
}

export interface HealthVerdict {
  tone: Tone;
  label: string;
  /** 判定の理由をラベルに収められないとき (サーバーの判定不能など) の補足 */
  detail?: string;
}

/**
 * 安全機構の異常の種別。**表示文字列と分けて持つ** —— `SubsystemStatus` は「無励磁の
 * まま」の行にだけ再励磁ボタンを添えるので、判定を `label` の文字列一致で書くと
 * **文言を 1 文字直しただけでボタンが消える**（型検査は通る）。union なら綴り間違いで
 * コンパイルが落ちる。
 */
export type SafetyIssueKind =
  | "unknown"
  | "sync_violation"
  | "unenergized"
  | "loops_stopped"
  | "monitors_stopped"
  | "refreshers_stopped";

/** 安全機構の異常 1 件。`hint` は操縦者が次に取るべき行動 */
export interface SafetyIssue {
  /** 機械可読の種別。UI の分岐はこれだけを見る (`label` は表示専用) */
  kind: SafetyIssueKind;
  label: string;
  detail: string;
  hint: string;
}

/**
 * 再励磁がサーバー側で処理中か。押した記憶や `unenergized_motors` の中身から導出すると
 * サーバーが拒否した押下まで「処理中」に見えるので、配信された欄をそのまま読む。
 *
 * 読めなかった配信 (`MALFORMED`) と未配信は false —— そのとき `describeSafetyIssues` が
 * 「安全機構 判定不能」だけを返して再励磁ボタンごと出さない (異常側へ倒す責務はあちら)。
 */
export function isReenergizePending(safety: SafetyPayload | undefined): boolean {
  if (safety === undefined || safety === MALFORMED) return false;
  return safetyShapeErrors(safety).length === 0 && safety.reenergizing;
}

/** モータ温度の色分けに使うしきい値 [℃]。正はサーバーの config にしかない */
export interface TempThresholds {
  warning: number;
  critical: number;
}

/**
 * `server_info` の 2 値からしきい値を作る。片方でも欠けていたら null。
 *
 * 片方だけで判定すると「warning は出ないのに danger だけ出る」中途半端な色分けになり、
 * 画面からはしきい値が届いていないことも読み取れない。揃っているときだけ判定する。
 */
export function tempThresholdsOf(serverInfo: ServerInfo | undefined): TempThresholds | null {
  const warning = serverInfo?.temp_warning_c;
  const critical = serverInfo?.temp_critical_c;
  if (typeof warning !== "number" || typeof critical !== "number") return null;
  return { warning, critical };
}

/**
 * モータ一覧の見出しチップ (MotorSummary) の判定。判定を MotorSummary 側に置くと、
 * 同じ画面に並ぶ 3 つの表示 (見出しチップ・各行のバッジ・このサマリー) が別々の根拠で
 * 答えることになる。
 *
 * **入力はサーバーのモータ健全性だけで、温度テレメトリは見ない** —— 温度警告は
 * サーバーが config の `temp_warning_c` で既に `warning` を立てており、UI が別の
 * しきい値で重ねて数えるとサーバー判定と食い違った件数が画面に出る。
 * 未配信・空配列を success へ倒さないのは `evaluateHealth` と同じ理由。
 */
export function summarizeMotors(healthMotors: MotorHealth[] | undefined): HealthVerdict {
  if (!healthMotors || healthMotors.length === 0) {
    return { tone: "neutral", label: "ヘルス未取得" };
  }

  const anomalies = healthMotors.filter((m) => m.state !== "ok");
  if (anomalies.length === 0) return { tone: "success", label: "All operational" };

  const tone: Tone = anomalies.some((m) => m.state === "fault") ? "error" : "warning";
  return { tone, label: `異常 ${anomalies.length} 件` };
}

/**
 * モータ温度の状態色。判定を画面ごとに書き写すと、色の語彙もしきい値の解釈も画面ごとに
 * ずれ、config を直しても片方の画面にしか効かない。
 *
 * しきい値は `server_info` 由来のものしか使わない (docs/invariants.md 「UI はしきい値の
 * フォールバック値を持たない」)。届いていない間は `neutral` へ倒す。
 */
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

/**
 * CAN 受信の途絶がワーク落下に繋がりうるバスを挙げる。平常時は空配列。
 *
 * 電磁弁基板は「止める = 消磁」の一手しか持たず、コマンドウォッチドッグ (既定 500ms) の
 * 満了で吸着中のワークが落ちる。CAN が 1 秒弱止まればまず満了する。**判定はサーバー
 * (`may_affect_workpiece`) が持ち、ここはその値を読むだけ** —— バス名やドライバ種別を
 * UI へ書き写すと、弁のバスを config で変えた瞬間に判定が古いまま残る。
 *
 * **`BusHealth.state` の判定には触れない。** バスが復旧して `ok` に戻ってもエピソード数
 * (`rx_down_episodes`) は試合中ずっと残るので、この一覧は `evaluateHealth` の判定 (`tone`)
 * とは独立に存在する —— ここが空でなくてもあちらの結論を上書きしてはならない。
 */
export function workpieceRiskBuses(health: HealthPayload | undefined): BusHealth[] {
  const readable = readableHealth(health);
  if (!readable) return [];
  return readable.buses.filter((b) => b.may_affect_workpiece && b.rx_down_episodes > 0);
}

/**
 * 起動の猶予を過ぎても自己申告 (`INFO`) を一度も受けていない自作モタドラ。平常時は空配列。
 *
 * **これは「壊れている」ではない。** `INFO` の未受信は送信バッファの都合でも起きるので
 * FAULT にしない。空でなくても焼き忘れ検出 (`info_mismatch`) が沈黙しているだけなので、
 * `describeSafetyIssues` には含めず `evaluateHealth` の判定 (`tone`) も動かさない
 * (`workpieceRiskBuses` と同じ位置付け)。
 */
export function firmwareUnconfirmedMotors(safety: SafetyPayload | undefined): string[] {
  if (!safety || safety === MALFORMED) return [];
  if (safetyShapeErrors(safety).length > 0) return [];
  return safety.firmware_unconfirmed_motors;
}

/**
 * 投げっぱなしタスク (`RobotServer.watch_task`) が拾った失敗ラベル。平常時は空配列。
 *
 * **これも「壊れている」ではない。** 一度失敗したことがある、というだけの記録で復帰しても
 * 消えない (試合開始まで残る)。過去の 1 回の失敗で機体が今も壊れているとは言い切れないので、
 * `describeSafetyIssues` には含めず `evaluateHealth` の判定 (`tone`) も動かさない。
 */
export function failedTasks(safety: SafetyPayload | undefined): string[] {
  if (!safety || safety === MALFORMED) return [];
  if (safetyShapeErrors(safety).length > 0) return [];
  return safety.failed_tasks;
}

/**
 * 安全機構を判定できなかったことを、異常 1 件として出す。「読めなかったから何も出さない」
 * は最悪の選択肢になる —— 同期ずれラッチも保護ループの停止も検知できていないのに、
 * 画面は平常時と 1 ピクセルも変わらない。
 */
function safetyUnknown(detail: string): SafetyIssue {
  return {
    kind: "unknown",
    label: "安全機構 判定不能",
    detail,
    hint: "安全機構の配信を読めていません。同期ずれラッチも保護ループの停止も検知できない状態です — 機体を動かす前にサーバーのログを確認してください",
  };
}

/**
 * 安全機構の異常を列挙する。平常時は空配列 (画面に何も足さない)。
 *
 * ラッチ中の軸が分からないと操縦者は復旧手順を選べず、200Hz の位置制御ループ・50Hz の
 * 同期監視・20Hz の目標値再送が死んだことは配信を読まない限り誰も気付けない (WS は
 * 繋がったままモータ状態も届き続けるため、画面は正常に見える)。
 *
 * 集約値 (`*_running`) と内訳 (`position_loops` 等) は同じ事実の 2 つの見え方なので、
 * 1 タスク種別につき 1 件へ畳む。
 */
export function describeSafetyIssues(safety: SafetyPayload | undefined): SafetyIssue[] {
  if (!safety) return [];

  // 受信境界 (`parseSafety`) を通っていても props で受け取る経路 (SubsystemStatus) は
  // 残る。型は実行時に消えるので、ここでも形を確かめる。**欠けた欄を `?? []` や
  // `?? false` で埋めてはならない** (docs/invariants.md 「受信境界では『読めなかった
  // 配信』を `MALFORMED` として異常側へ倒す」)
  if (safety === MALFORMED) return [safetyUnknown("安全機構の配信全体")];
  const broken = safetyShapeErrors(safety);
  if (broken.length > 0) return [safetyUnknown(broken.join(", "))];

  const issues: SafetyIssue[] = [];

  if (safety.sync_violations.length > 0) {
    issues.push({
      kind: "sync_violation",
      label: "同期ずれラッチ",
      detail: safety.sync_violations.join(", "),
      hint: "機構を直してから緊急停止を解除し直してください (解除しただけでは動きません)",
    });
  }

  // 緊急停止は解除されているのに励磁が戻っていない。指令は 20Hz で飛び続け、
  // フィードバックもヘルスも正常なので、ここで言わないと誰も気付けない
  if (safety.unenergized_motors.length > 0) {
    issues.push({
      kind: "unenergized",
      label: "無励磁のまま",
      detail: safety.unenergized_motors.join(", "),
      hint: "指令は届いていますが励磁されていません。操縦者画面の「再励磁」ボタンを押してください (直らなければ緊急停止をもう一度押して解除し直すか、ドライバの電源と CAN 配線を確認)",
    });
  }

  // paused は動作確認中の意図的な停止なので異常に数えない
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

  // 20Hz の再送が止まると 500ms 後にファーム側のコマンドウォッチドッグが働き、
  // generic アクチュエータ (グリッパ・コンベア・壁) が一斉に停止する
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

/**
 * CAN・モータ・安全機構の状態を 1 つの判定へ畳む。判定をここへ集約する理由は
 * docs/invariants.md 「機体の健全性判定は 1 箇所だけが持つ」。
 *
 * 安全機構をバス・モータより先に見るのは、復旧操作が別物だから —— ラッチ中の軸は
 * 緊急停止を解除しても動かず、CAN やモータの表示を見ても理由が分からない。
 *
 * **サーバーの `overall` より楽観的な結論を出してはならない** —— サーバーは健全性を
 * 計算できなかったときに overall=down・内訳空で「判定不能」を配信するので、内訳だけを
 * 見て「異常なし」を返すとそのフェイルセーフが画面上で消える。
 *
 * **温度テレメトリは入力に取らない** (`summarizeMotors` と同じ理由)。
 *
 * **`connected` を必ず渡す。** 切断中の判定は「通信が切れた瞬間の値」であって今の機体
 * ではない。既定値を持たせて省略できるようにすると、書き忘れた画面だけが凍った緑の
 * 「異常なし」を出し続ける。
 */
export function evaluateHealth(
  health: HealthPayload | undefined,
  safety: SafetyPayload | undefined,
  connected: boolean,
): HealthVerdict {
  // 通信が落ちている間、手元にあるのは切れた瞬間の値でしかない。緑の「異常なし」を
  // 出し続けると、操縦者はそれを今の機体の状態として読む。色は付けない (neutral) ——
  // 異常だと言い切ることもできないため
  if (!connected) {
    return {
      tone: "neutral",
      label: "通信断のため判定不能",
      detail: "サーバーと切断しています。表示は切断時点の値で、今の機体の状態ではありません",
    };
  }

  // 判定と詳細表示を同じ列挙から作る。チップは「種別 + 対象」、詳細行は復旧手順を担う
  const [safetyIssue] = describeSafetyIssues(safety);
  if (safetyIssue) {
    return { tone: "error", label: `${safetyIssue.label} ${safetyIssue.detail}` };
  }

  if (!health) return { tone: "neutral", label: "ヘルス未取得" };

  // 受信境界 (`parseHealth`) を通っていても props で受け取る経路 (SubsystemStatus) は
  // 残る。**`?? []` で埋めてはならない** —— 埋めると「バスが 1 本も無いから異常なし」に
  // 化け、埋めたこと自体が画面から読めなくなる
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

  // 内訳から理由を挙げられないのに overall が down = サーバーが健全性を判定できていない
  // (`lib/server.py` の `_health_unknown`)。success を返すと、判定不能を DOWN へ倒す
  // サーバーのフェイルセーフを UI 側が打ち消してしまう
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
