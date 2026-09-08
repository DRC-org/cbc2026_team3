import {
  ChevronDown,
  ChevronRight,
  ListX,
  PackageX,
  ShieldAlert,
  ShieldQuestion,
} from "lucide-react";
import { useEffect, useId, useState } from "react";

import { HealthIndicator } from "@/components/diagnostics/HealthIndicator";
import { MotorSummary } from "@/components/diagnostics/MotorSummary";
import { SensorSummary } from "@/components/diagnostics/SensorSummary";
import type { SensorPayload } from "@/components/diagnostics/SensorSummary";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { StatusBadge } from "@/components/ui/StatusBadge";
import {
  describeSafetyIssues,
  evaluateHealth,
  failedTasks,
  firmwareUnconfirmedMotors,
  isReenergizePending,
  readableHealth,
  workpieceRiskBuses,
} from "@/lib/healthVerdict";
import type { HealthPayload, SafetyPayload, TempThresholds } from "@/lib/healthVerdict";
import type { BusHealth, MotorState } from "@/lib/protocol";

interface SubsystemStatusProps {
  /** 受信境界を通っていない props 経路が残るので、読めなかった配信も受ける */
  health: HealthPayload | undefined;
  motors: Record<string, MotorState>;
  /** 安全機構 (同期ずれラッチ・保護ループの生死)。未受信でも表示は成立する */
  safety?: SafetyPayload;
  /**
   * 自作基板のセンサ入力 (原点スイッチ)。**モータ一覧とは別に描く。**
   * 未配信・センサ無しの構成では 1px も占めない
   */
  sensors?: SensorPayload;
  /**
   * 温度の色分けに使うしきい値 (正はサーバーの config)。末端の表示部品が context を
   * 読み始めると `health` / `motors` を props で受けている一貫性が崩れるので、
   * 呼び出し元が渡す。
   */
  tempThresholds?: TempThresholds | null;
  /**
   * サーバーと繋がっているか。**省略できない** —— 渡し忘れた画面だけが凍った緑の
   * 「異常なし」を出し続ける。
   */
  connected: boolean;
  /** 準備中は中身を開いた状態から始める（配線確認が目的のフェーズなので） */
  defaultOpen?: boolean;
  /**
   * 判定チップと開閉見出しを出すか。同じ画面で別の要素 (StartGate) が既に「異常が
   * あるか」を答えている場合は false —— 同じ文字列を 2 度並べると、操縦者はどちらが
   * 最新か確かめる往復を強いられる。
   */
  showVerdict?: boolean;
  /**
   * 励磁が落ちたモータを戻す (`reenergize_motors`)。渡した画面だけボタンが出る。
   * **可否の判定はここに持たせない** —— 押せば送るだけで、拒否はサーバーが理由付きで
   * 返す。渡さない画面 (Monitor) ではボタンごと出さず、操縦者画面へ促す文言だけを
   * `describeSafetyIssues` の hint が持つ。
   */
  onReenergize?: () => void;
}

/**
 * CAN 途絶がワーク落下に繋がりうるバスの一覧。平常時 (0 件) は何も出さない。
 *
 * `evaluateHealth` の判定 (`tone`) を経由しない —— `BusHealth.state` は復旧すれば `ok` へ
 * 戻るが、この一覧が示す「試合中に何回起きたか」は 0 に戻らない
 * (`docs/checks_and_health.md`)。判定を動かさず情報を 1 つ足すだけの独立表示。
 */
function WorkpieceRiskNotice({ buses }: { buses: BusHealth[] }) {
  if (buses.length === 0) return null;

  return (
    <ul className="flex shrink-0 flex-col gap-1 border-l-[0.25rem] border-l-warning bg-warning/5 px-2 py-1">
      {buses.map((bus) => (
        <li key={bus.name} className="flex min-w-0 flex-col">
          <span className="flex min-w-0 items-center gap-1.5">
            <Icon as={PackageX} className="shrink-0 text-warning" />
            <StatusBadge tone="warning">CAN 途絶 {bus.rx_down_episodes}回</StatusBadge>
            <span className="min-w-0 truncate font-mono text-base-content/80">{bus.name}</span>
          </span>
          {/* 状態だけ出しても操縦者は何が起きたか分からない。理由まで書く */}
          <span className="pl-[1.4rem] text-[0.85em] text-base-content/70">
            吸着していたワークが落ちた可能性があります (基板のコマンドウォッチドッグが満了)
          </span>
        </li>
      ))}
    </ul>
  );
}

/**
 * 起動の猶予を過ぎても `INFO` を一度も受けていない自作モタドラの一覧。平常時 (0 件) は
 * 何も出さない。
 *
 * **「異常」として赤くしない** —— 空でないのは「焼き忘れ検出 (info_mismatch) が今は
 * 働いていない」という事実で、機体そのものが壊れているとは限らない。
 *
 * **1 行にまとめず、モータごとに `<li>` を並べる。** 現実的なきっかけは「1 枚の基板が
 * 丸ごと `INFO` を出していない」なので電磁弁 6ch が同時に並び、`join(", ")` の 1 行では
 * 途中で切れて操縦者が指差喚呼 (`firmware_match`) に答えられない。
 */
function FirmwareUnconfirmedNotice({ motors }: { motors: string[] }) {
  if (motors.length === 0) return null;

  return (
    <ul className="flex shrink-0 flex-col gap-1 border-l-[0.25rem] border-l-info bg-info/5 px-2 py-1">
      {motors.map((motor) => (
        <li key={motor} className="flex min-w-0 flex-col">
          <span className="flex min-w-0 items-center gap-1.5">
            <Icon as={ShieldQuestion} className="shrink-0 text-info" />
            <StatusBadge tone="info">版番号 未確認</StatusBadge>
            <span className="min-w-0 truncate font-mono text-base-content/80">{motor}</span>
          </span>
          {/* **電源・CAN 配線を疑わせてはならない** —— サーバーは FEEDBACK が届いて
              いるモータだけをここへ載せるので、配線を見ても必ず何も見つからない
              (基板が落ちている場合は `evaluateHealth` が STALE として別に主張する) */}
          <span className="pl-[1.4rem] text-[0.85em] text-base-content/70">
            FEEDBACK は届くのに INFO が来ません。ファームを焼き直して candump で確認してください
          </span>
        </li>
      ))}
    </ul>
  );
}

/**
 * 投げっぱなしタスク (`asyncio.create_task` して待たないもの) が失敗したラベルの一覧。
 * 平常時 (0 件) は何も出さない。
 *
 * **「異常」として赤くしない** (`FirmwareUnconfirmedNotice` と同じ位置付け)。1 度失敗した
 * 記録で、直っていても試合開始まで消えない (`docs/checks_and_health.md`)。
 *
 * **再起動を促さない。** CAN や機体の異常ではなく内部例外なので、トレースバックも
 * 直す手がかりも journal にしかない。
 */
function FailedTasksNotice({ labels }: { labels: string[] }) {
  if (labels.length === 0) return null;

  return (
    <ul className="flex shrink-0 flex-col gap-1 border-l-[0.25rem] border-l-info bg-info/5 px-2 py-1">
      {labels.map((label) => (
        <li key={label} className="flex min-w-0 flex-col">
          <span className="flex min-w-0 items-center gap-1.5">
            <Icon as={ListX} className="shrink-0 text-info" />
            <StatusBadge tone="info">タスク失敗</StatusBadge>
            <span className="min-w-0 truncate text-base-content/80">{label}</span>
          </span>
          <span className="pl-[1.4rem] text-[0.85em] text-base-content/70">
            再起動ではなく journal (journalctl -u cbc-control) で原因を確認してください
          </span>
        </li>
      ))}
    </ul>
  );
}

/**
 * 安全機構の異常。平常時は 1 件も出ない。
 *
 * ラッチ中の軸は緊急停止を解除しても動かず、保護ループが死んでも WS は繋がったまま
 * モータ状態が届き続ける。どちらも「画面が正常に見えるのに機体は正常でない」型の異常で、
 * 自分から主張しない限り誰も気付けない。
 */
function SafetyIssues({
  safety,
  onReenergize,
}: {
  safety: SafetyPayload | undefined;
  onReenergize?: () => void;
}) {
  const issues = describeSafetyIssues(safety);
  // 在飛中かはサーバーが配る。押した記憶から組み立てると拒否された押下まで
  // 「処理中」に見える
  const pending = isReenergizePending(safety);
  if (issues.length === 0) return null;

  return (
    <ul className="flex shrink-0 flex-col gap-1 border-l-[0.25rem] border-l-error bg-error/5 px-2 py-1">
      {issues.map((issue) => (
        <li key={issue.label} className="flex min-w-0 flex-col">
          <span className="flex min-w-0 items-center gap-1.5">
            <Icon as={ShieldAlert} className="shrink-0 text-error" />
            <span className="shrink-0 font-medium">{issue.label}</span>
            <span className="min-w-0 truncate font-mono text-base-content/80">{issue.detail}</span>
          </span>
          {/* 状態だけ出しても操縦者は次の一手を選べない。復旧手順まで書く */}
          <span className="pl-[1.4rem] text-[0.85em] text-base-content/70">{issue.hint}</span>
          {/* 押せる場所は限定する — この異常が実際に出ていて、かつこの画面に
              コールバックが渡されているとき (操縦者自身の画面) だけ */}
          {issue.kind === "unenergized" && onReenergize ? (
            <Button
              tone="warn"
              className="ml-[1.4rem] self-start"
              onClick={onReenergize}
              disabled={pending}
            >
              {pending ? "処理中…" : "再励磁"}
            </Button>
          ) : null}
        </li>
      ))}
    </ul>
  );
}

/**
 * 診断情報の累進的開示。平常時は 1 行に畳み、異常が出たときだけ自分から開いて主張する
 * (docs/invariants.md 「平常時に静かで、異常時に自分から主張する」)。
 */
export function SubsystemStatus({
  health,
  motors,
  safety,
  sensors,
  connected,
  tempThresholds = null,
  defaultOpen = false,
  showVerdict = true,
  onReenergize,
}: SubsystemStatusProps) {
  const verdict = evaluateHealth(health, safety, connected);
  // 内訳を並べる部品は「読めなかった」を表現できない。判定 (上) だけがそれを担う
  const readable = readableHealth(health);
  const riskyBuses = workpieceRiskBuses(health);
  const unconfirmedMotors = firmwareUnconfirmedMotors(safety);
  const failedTaskLabels = failedTasks(safety);
  const [manualOpen, setManualOpen] = useState(defaultOpen);
  // **`defaultOpen` は初期値ではなく「今このパネルを開いておくべきか」の宣言。**
  // 呼び出し側 (`RobotControl`) は手動操縦へ切り替わったときに false → true で渡し直すが、
  // この部品は grid の同じ位置・同じ型のまま残るので**再マウントされない** ——
  // `useState` の初期値として受けるだけだと、試合中に手動へ入っても畳まれたままになる。
  // 宣言が変わった周期だけ追従するので、操縦者が手で畳んだ状態は保たれる。
  useEffect(() => setManualOpen(defaultOpen), [defaultOpen]);
  // 開閉ボタンと開閉対象を結ぶ。aria-expanded だけでは「何が開くのか」が伝わらない
  const detailsId = useId();

  // 異常時は操縦者の開閉操作より優先して開く。畳んだまま見逃させない。ワーク落下の
  // 恐れも同格 —— `verdict.tone` はバスが復旧すれば平常に戻るが、こちらは試合中ずっと
  // 主張し続けるべき情報なので、判定 (tone) を変えずにここへ OR で足す。
  //
  // **版番号未確認 (`unconfirmedMotors`) はここに含めない** —— 試合中の 1 事象ではなく
  // 「その基板は焼き忘れ検出そのものが効かない」という変わらない状態で、しかも操縦者は
  // 試合中に直せない。畳めるままにして、開いたときに見える情報として残す
  const forcedOpen =
    verdict.tone === "error" || verdict.tone === "warning" || riskyBuses.length > 0;
  const open = !showVerdict || forcedOpen || manualOpen;

  const busCount = readable?.buses.length ?? 0;
  const motorCount = Object.keys(motors).length;

  return (
    <div className="flex min-h-0 flex-col">
      {showVerdict ? (
        <button
          type="button"
          // 記録するのは「今の見え方の逆」。強制開示中に (v) => !v で反転させると、
          // 見た目が開いたままなのに内部だけ「開く」へ倒れ、異常が解消した後も
          // 数字が並んだまま試合の残り時間ずっと開きっぱなしになる
          onClick={() => setManualOpen(!open)}
          aria-expanded={open}
          aria-controls={detailsId}
          className="flex shrink-0 cursor-pointer items-center gap-2 px-1 py-1 text-left hover:bg-base-200"
        >
          <Icon as={open ? ChevronDown : ChevronRight} className="text-base-content/60" />
          <StatusBadge tone={verdict.tone}>{verdict.label}</StatusBadge>
          <span className="min-w-0 flex-1 truncate text-base-content/70">
            CAN {busCount} · モータ {motorCount}
          </span>
        </button>
      ) : null}

      {open ? (
        <div id={detailsId} className="flex min-h-0 flex-1 flex-col gap-1 pt-1">
          {/* 判定の理由をラベルへ収められなかった場合の逃し先。
              サーバーが「判定不能」を配信したときの原因文はここにしか残らない */}
          {verdict.detail ? (
            <p className="shrink-0 border-l-[0.25rem] border-l-error bg-error/5 px-2 py-1">
              {verdict.detail}
            </p>
          ) : null}
          <WorkpieceRiskNotice buses={riskyBuses} />
          <FirmwareUnconfirmedNotice motors={unconfirmedMotors} />
          <FailedTasksNotice labels={failedTaskLabels} />
          <SafetyIssues safety={safety} onReenergize={onReenergize} />
          <HealthIndicator health={readable} />
          {/* モータより前に置く。モータ一覧は残り高さいっぱいまで伸びてスクロールするので、
              後ろへ回すと本数によっては指差喚呼で見たい 1 行が畳まれた先に隠れる */}
          <SensorSummary sensors={sensors} />
          <MotorSummary
            motors={motors}
            healthMotors={readable?.motors}
            tempThresholds={tempThresholds}
          />
        </div>
      ) : null}
    </div>
  );
}
