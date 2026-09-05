import { ChevronDown, ChevronRight, PackageX, ShieldAlert } from "lucide-react";
import { useId, useState } from "react";

import { HealthIndicator } from "@/components/diagnostics/HealthIndicator";
import { MotorSummary } from "@/components/diagnostics/MotorSummary";
import { SensorSummary } from "@/components/diagnostics/SensorSummary";
import type { SensorPayload } from "@/components/diagnostics/SensorSummary";
import { Icon } from "@/components/ui/Icon";
import { StatusBadge } from "@/components/ui/StatusBadge";
import {
  describeSafetyIssues,
  evaluateHealth,
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
   * 温度の色分けに使うしきい値。正はサーバーの config で、`server_info` から届く。
   * 末端の表示部品が context を読み始めると `health` / `motors` を props で受けている
   * 現在の一貫性が崩れ、テストのたびに Provider が要る。呼び出し元が渡す。
   */
  tempThresholds?: TempThresholds | null;
  /**
   * サーバーと繋がっているか。切断中の判定は切れた瞬間の値でしかないので、
   * `evaluateHealth` が「通信断のため判定不能」へ倒す。**省略できない** ——
   * 渡し忘れた画面だけが凍った緑の「異常なし」を出し続ける
   */
  connected: boolean;
  /** 準備中は中身を開いた状態から始める（配線確認が目的のフェーズなので） */
  defaultOpen?: boolean;
  /**
   * 判定チップと開閉見出しを出すか。
   * 同じ画面で別の要素 (StartGate) が既に「異常があるか」を答えている場合は false。
   * 同じ文字列を 2 度並べると、操縦者はどちらが最新か確かめる往復を強いられる。
   */
  showVerdict?: boolean;
}

/**
 * CAN 途絶がワーク落下に繋がりうるバスの一覧。平常時 (0 件) は何も出さない。
 *
 * `evaluateHealth` の判定 (`tone`) を経由しない ——
 * `BusHealth.state` は復旧すれば `ok` へ戻るが、この一覧が示す「試合中に
 * 何回起きたか」は 0 に戻らない (`docs/checks_and_health.md` 参照)。判定その
 * ものを動かさず、情報を 1 つ足すだけに留めるための独立表示。
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
 * 安全機構の異常。平常時は 1 件も出ない。
 *
 * ラッチ中の軸は緊急停止を解除しても動かず、保護ループが死んでも WS は繋がったまま
 * モータ状態が届き続ける。どちらも「画面が正常に見えるのに機体は正常でない」型の異常で、
 * 自分から主張しない限り誰も気付けない。
 */
function SafetyIssues({ safety }: { safety: SafetyPayload | undefined }) {
  const issues = describeSafetyIssues(safety);
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
        </li>
      ))}
    </ul>
  );
}

/**
 * 診断情報の累進的開示。
 *
 * 試合中の操縦者は機体を見ており、画面へ視線を戻すのは一瞬しかない。そこに
 * 8 モータ × 4 値 = 32 個の数字が常時出ていると、本当に必要な「異常があるか」が
 * 数字の海に沈む。しかも試合中にこれらの数値を見て取れる行動は無い。
 * 平常時は 1 行に畳み、異常が出たときだけ自分から開いて主張する。
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
}: SubsystemStatusProps) {
  const verdict = evaluateHealth(health, safety, connected);
  // 内訳を並べる部品は「読めなかった」を表現できない。判定 (上) だけがそれを担う
  const readable = readableHealth(health);
  const riskyBuses = workpieceRiskBuses(health);
  const [manualOpen, setManualOpen] = useState(defaultOpen);
  // 開閉ボタンと開閉対象を結ぶ。aria-expanded だけでは「何が開くのか」が伝わらない
  const detailsId = useId();

  // 異常時は操縦者の開閉操作より優先して開く。畳んだまま見逃させない。
  // ワーク落下の恐れも同格 —— `verdict.tone` はバスが復旧すれば平常に戻るが、
  // こちらは試合中ずっと自分から主張し続けるべき情報なので、判定 (tone) を
  // 変えずにここへ OR で足す
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
          <SafetyIssues safety={safety} />
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
