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
 * 内訳を並べる部品 (`HealthIndicator` / `MotorSummary`) は「読めなかった」を
 * 表現する手段を持たないので、そこへ渡す前にここで落とす。**判定側
 * (`evaluateHealth`) は落とさない** —— あちらは MALFORMED を異常として出す役。
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

/** 安全機構の異常 1 件。`hint` は操縦者が次に取るべき行動 */
export interface SafetyIssue {
  label: string;
  detail: string;
  hint: string;
}

/**
 * 「無励磁のまま」issue の固定ラベル。`SubsystemStatus` が再励磁ボタンを
 * どの issue に添えるか判定するのに使う。文言をここ 1 箇所にしておかないと、
 * 打鍵ミスでボタンが出なくなっても型検査を通ってしまう。
 */
export const UNENERGIZED_ISSUE_LABEL = "無励磁のまま";

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
 * モータ一覧の見出しチップ (MotorSummary) の判定。
 *
 * 判定を MotorSummary 側に置くと、同じ画面に並ぶ 3 つの表示 —— 診断カラムの
 * 見出しチップ (evaluateHealth)・各行のバッジ (MotorHealth.state)・このサマリー ——
 * が別々の根拠で答えることになる。実際にサマリーだけが温度しきい値しか見ておらず、
 * FAULT のモータが行では赤バッジなのにサマリーは緑の「All operational」を出していた。
 *
 * **入力はサーバーのモータ健全性だけで、温度テレメトリは見ない。** 温度警告は
 * サーバーが config の `temp_warning_c` で既に `warning` を立てている。UI が別の
 * しきい値で重ねて数えると、サーバー判定と食い違った件数が画面に出る。
 *
 * 未配信・空配列を success へ倒さないのは `evaluateHealth` と同じ理由で、
 * 「異常の有無が分からない」は安全側では異常であって正常ではない。
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
 * モータ温度の状態色。
 *
 * 以前 `MotorStatus` と `MotorTuning` に別実装があり、片方は独自の
 * `StatTone`、もう片方は `Tone` を返していた。しきい値の変更が片方にしか
 * 効かない構造だったので、判定はここ 1 箇所に置く。
 *
 * **しきい値は `server_info` 由来のものしか使わず、UI 側のフォールバック値を持たない。**
 * 持つと、config を変えても画面だけが古い境界で判定する二重管理が戻る。届いていない間は
 * `neutral` (色を付けない) にする —— 適当な既定値で「正常」とも「警告」とも言わない。
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
 * 電磁弁基板は「止める = 消磁」の一手しか持たず、コマンドウォッチドッグ
 * (既定 500ms) が満了すると吸着中のワークが落ちる。CAN が 1 秒弱止まれば
 * まず満了するので、`can_generic` (弁が載るバス) の途絶はワーク落下を疑うが、
 * `can_dm3520` や `can_m3508` の途絶では落ちない。**判定はサーバー
 * (`may_affect_workpiece`) が持ち、ここではその値をそのまま読むだけにする** ——
 * バス名やドライバ種別を UI へ書き写すと、弁のバスを config で変えた瞬間に
 * 判定が古いまま残る (`healthVerdict.ts` に判定を集約する既存の原則と同じ)。
 *
 * **`BusHealth.state` (OK/DEGRADED/DOWN) の判定そのものには触れない。** バスが
 * 復旧して `ok` に戻ってもエピソード数 (`rx_down_episodes`) は試合中ずっと
 * 残るので、この一覧も `evaluateHealth` の判定 (`tone`) とは独立に存在する ——
 * ここが空でなくても `evaluateHealth` の結論を上書きしてはならない。
 */
export function workpieceRiskBuses(health: HealthPayload | undefined): BusHealth[] {
  const readable = readableHealth(health);
  if (!readable) return [];
  return readable.buses.filter((b) => b.may_affect_workpiece && b.rx_down_episodes > 0);
}

/**
 * 安全機構を判定できなかったことを、異常 1 件として出す。
 *
 * 「読めなかったから何も出さない」は最悪の選択肢になる —— 同期ずれラッチも
 * 保護ループの停止も検知できていないのに、画面は平常時と 1 ピクセルも変わらない。
 */
function safetyUnknown(detail: string): SafetyIssue {
  return {
    label: "安全機構 判定不能",
    detail,
    hint: "安全機構の配信を読めていません。同期ずれラッチも保護ループの停止も検知できない状態です — 機体を動かす前にサーバーのログを確認してください",
  };
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
export function describeSafetyIssues(safety: SafetyPayload | undefined): SafetyIssue[] {
  if (!safety) return [];

  // 受信境界 (`parseSafety`) を通っていても、`safety` を props で受け取る経路
  // (SubsystemStatus) は残る。型は実行時に消えるので、ここでも形を確かめる。
  //
  // **欠けた欄を `?? []` や `?? false` で埋めてはならない。** 埋めた瞬間に
  // 「ラッチしているのに画面は平常」へ化け、埋めたことは画面から読めない。
  // サーバーの overall=down を「健全性 判定不能」へ倒すのと同じ扱いにする。
  if (safety === MALFORMED) return [safetyUnknown("安全機構の配信全体")];
  const broken = safetyShapeErrors(safety);
  if (broken.length > 0) return [safetyUnknown(broken.join(", "))];

  const issues: SafetyIssue[] = [];

  if (safety.sync_violations.length > 0) {
    issues.push({
      label: "同期ずれラッチ",
      detail: safety.sync_violations.join(", "),
      hint: "機構を直してから緊急停止を解除し直してください (解除しただけでは動きません)",
    });
  }

  // 緊急停止は解除されているのに励磁が戻っていない。指令は 20Hz で飛び続け、
  // フィードバックもヘルスも正常なので、ここで言わないと誰も気付けない。
  // 復帰させる操作 (reenergize_motors) はこの機体の操縦者画面にしか無い
  // (Monitor はどのロボットの画面かを跨いで表示するため、ここではボタンでなく
  // 導線だけを言葉で示す)
  if (safety.unenergized_motors.length > 0) {
    issues.push({
      label: UNENERGIZED_ISSUE_LABEL,
      detail: safety.unenergized_motors.join(", "),
      hint: "指令は届いていますが励磁されていません。操縦者画面の「再励磁」ボタンを押してください (直らなければ緊急停止をもう一度押して解除し直すか、ドライバの電源と CAN 配線を確認)",
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
 *
 * **温度テレメトリは入力に取らない。** 高温は config の `temp_warning_c` を見た
 * サーバーが `MotorHealth.state = warning` として既に配信している。UI が別途数えると
 * 同じ 1 基が 2 件として計上され、しかも UI 側のしきい値がサーバーとずれていれば
 * 件数そのものがサーバー判定と食い違う。数えるのは配信された健全性だけにする。
 *
 * **`connected` を必ず渡す。** 切断中の判定は「通信が切れた瞬間の値」であって
 * 今の機体ではない。既定値を持たせて省略できるようにすると、書き忘れた画面だけが
 * 凍った緑の「異常なし」を出し続ける (`motorCheckStatus` が切断を判定へ織り込んで
 * いるのと同じ理由で、ここも呼び出し側に宣言させる)。
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

  // 受信境界 (`parseHealth`) を通っていても、`health` を props で受け取る経路
  // (SubsystemStatus) は残る。型は実行時に消えるので、ここでも形を確かめる。
  // **`?? []` で埋めてはならない** —— 埋めると「バスが 1 本も無いから異常なし」に
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
