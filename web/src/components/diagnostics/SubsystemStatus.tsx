import {
  ChevronDown,
  ChevronRight,
  Fence,
  ListX,
  PackageX,
  ShieldAlert,
  ShieldOff,
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
import { cx } from "@/lib/cx";
import {
  describeSafetyIssues,
  evaluateHealth,
  failedTasks,
  firmwareUnconfirmedMotors,
  isReenergizePending,
  limitBlindSensors,
  limitLatchedAxes,
  readableHealth,
  workpieceRiskBuses,
} from "@/lib/healthVerdict";
import type {
  HealthPayload,
  LimitLatchedAxis,
  SafetyPayload,
  TempThresholds,
} from "@/lib/healthVerdict";
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
  /**
   * 励磁が落ちたモータを戻す (`reenergize_motors`)。渡した画面だけボタンが出る。
   * **可否の判定はここに持たせない** — 押せば送るだけで、拒否は
   * サーバーが理由付きで返す (`MotorCheckController.deny_reason()` と同じ原則)。
   * 渡さない画面 (Monitor) ではボタンごと出さない — この機体の操縦者画面が
   * 別に居るので、そちらへ促す文言だけを `describeSafetyIssues` の hint が持つ。
   */
  onReenergize?: () => void;
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
    // 0 件で `<ul>` だけが残ると、中身の無い色付きの帯が平常時にずっと出る。
    // 名前を付けて「この一覧が居るかどうか」をテストから見えるようにしてある
    // (文字列の不在だけで見ると、早期リターンを消しても緑のまま通る)
    <ul
      aria-label="ワーク落下の恐れがあるバス"
      className="flex shrink-0 flex-col gap-1 border-l-[0.25rem] border-l-warning bg-warning/5 px-2 py-1"
    >
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
 * **「異常」として赤くしない** —— `evaluateHealth` の判定 (`tone`) はここを経由しない。
 * ここが空でないのは「焼き忘れ検出 (info_mismatch) が今は働いていない」という事実で、
 * 機体そのものが壊れているとは限らない。
 *
 * **1 行にまとめず、モータごとに `<li>` を並べる** (`WorkpieceRiskNotice` と同じ形)。
 * この状態が起きる最も現実的なきっかけは「1 枚の基板が丸ごと `INFO` を出していない」
 * なので、電磁弁 6ch やサブハンドの自作モタドラが同時に並ぶ。`join(", ")` の
 * 1 行では途中で切れ、操縦者は指差喚呼 (`firmware_match`) に答えられない。
 */
function FirmwareUnconfirmedNotice({ motors }: { motors: string[] }) {
  if (motors.length === 0) return null;

  return (
    <ul
      aria-label="版番号 未確認のモータ"
      className="flex shrink-0 flex-col gap-1 border-l-[0.25rem] border-l-info bg-info/5 px-2 py-1"
    >
      {motors.map((motor) => (
        <li key={motor} className="flex min-w-0 flex-col">
          <span className="flex min-w-0 items-center gap-1.5">
            <Icon as={ShieldQuestion} className="shrink-0 text-info" />
            <StatusBadge tone="info">版番号 未確認</StatusBadge>
            <span className="min-w-0 truncate font-mono text-base-content/80">{motor}</span>
          </span>
          {/* 状態だけ出しても操縦者は次の一手を選べない。手当てまで書く。
              **電源・CAN 配線を疑わせてはならない** —— サーバーは FEEDBACK が
              届いているモータだけをここへ載せる (`_firmware_unconfirmed_motors`) ので、
              配線を見ても必ず何も見つからない。基板が落ちている場合は
              `evaluateHealth` が STALE として別に主張する */}
          <span className="pl-[1.4rem] text-[0.85em] text-base-content/70">
            FEEDBACK は届くのに INFO が来ません。ファームを焼き直して candump で確認してください
          </span>
        </li>
      ))}
    </ul>
  );
}

/**
 * リミットスイッチ保護が今止めている軸の一覧。平常時 (0 件) は何も出さない。
 *
 * **接触そのものの再掲ではない。** センサが今 ON かは `SensorSummary` が既に描いて
 * いる。ここが出すのは「保護が発動して**その軸のその向きが止まっている**」という
 * 別の事実で、これが無いと操縦者から見えるのは「指令しても動かない」だけになる
 * (保護は軸ローカルで全体緊急停止に倒さないので、他の軸は平常どおり動く)。
 *
 * **「異常」として赤くしない** —— `evaluateHealth` の判定 (`tone`) はここを経由しない。
 * 保護が設計どおり働いた結果であって機体の故障ではないが、**逆向きへ退避するまでは
 * 解けない**ので、`WorkpieceRiskNotice` と同じ warning で自分から主張する。
 *
 * 軸名とセンサ名はサーバーが配るものをそのまま描く (UI 側に表を持たない)。
 */
function LimitLatchedNotice({ axes }: { axes: LimitLatchedAxis[] }) {
  if (axes.length === 0) return null;

  return (
    // 0 件で `<ul>` だけが残ると、中身の無い色付きの帯が平常時にずっと出る。
    // 名前を付けて「この一覧が居るかどうか」をテストから見えるようにしてある
    <ul
      aria-label="リミット到達の軸"
      className="flex shrink-0 flex-col gap-1 border-l-[0.25rem] border-l-warning bg-warning/5 px-2 py-1"
    >
      {axes.map((latched) => (
        <li key={latched.axis} className="flex min-w-0 flex-col">
          <span className="flex min-w-0 items-center gap-1.5">
            {/* 端に当たって進めない、を表す記号。緊急停止 (OctagonX) とも
                トリガー待ち (Hand) とも重ねない */}
            <Icon as={Fence} className="shrink-0 text-warning" />
            <StatusBadge tone="warning">リミット到達</StatusBadge>
            <span className="min-w-0 truncate font-mono text-base-content/80">{latched.axis}</span>
          </span>
          {/* 状態だけ出しても操縦者は次の一手を選べない。**どちらへ動かせるか**まで書く。
              どちらの端かはセンサ名にしか無いので必ず並べる */}
          <span className="pl-[1.4rem] text-[0.85em] text-base-content/70">
            {latched.sensors.join(", ")} に接触。この向きへの指令は止まります (逆向きへは動きます)
          </span>
        </li>
      ))}
    </ul>
  );
}

/**
 * フィードバック途絶でリミット保護が効いていないセンサの一覧。平常時 (0 件) は
 * 何も出さない。
 *
 * **「壊れている」ではなく「保護が働いていない」** —— `FirmwareUnconfirmedNotice` と
 * まったく同じ位置付けで、`evaluateHealth` の判定 (`tone`) は経由しない。途絶で軸を
 * 止めるとスイッチ 1 本の不調で試合中に機体が動かなくなるので、サーバーは判定しない
 * 方を選んでいる。そのぶん**効いていないことがここに出ていなければ、「守っている
 * つもり」の機体になる。**
 *
 * センサが途絶していること自体は STALE として別に主張されるので、文面は「保護が
 * 消えている」ことだけを言う (同じ事実を 2 度描かない)。
 */
function LimitBlindNotice({ sensors }: { sensors: string[] }) {
  if (sensors.length === 0) return null;

  return (
    <ul
      aria-label="リミット保護が無効なセンサ"
      className="flex shrink-0 flex-col gap-1 border-l-[0.25rem] border-l-info bg-info/5 px-2 py-1"
    >
      {sensors.map((sensor) => (
        <li key={sensor} className="flex min-w-0 flex-col">
          <span className="flex min-w-0 items-center gap-1.5">
            <Icon as={ShieldOff} className="shrink-0 text-info" />
            <StatusBadge tone="info">リミット保護 無効</StatusBadge>
            <span className="min-w-0 truncate font-mono text-base-content/80">{sensor}</span>
          </span>
          <span className="pl-[1.4rem] text-[0.85em] text-base-content/70">
            このスイッチは応答が途絶えており、触れても軸は止まりません
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
 * **「異常」として赤くしない** —— `evaluateHealth` の判定 (`tone`) はここを経由しない
 * (`FirmwareUnconfirmedNotice` と同じ位置付け)。緊急停止解除の再励磁・単発の再励磁・
 * 同期ずれ検出からの全体緊急停止のいずれかが 1 度失敗した記録で、直っていても
 * 試合開始まで消えない (`docs/checks_and_health.md` 参照)。
 *
 * **再起動を促さない。** これは CAN や機体の異常ではなく投げっぱなしタスクの内部例外
 * (トレースバックは journal にしか無い) なので、直す手がかりは journal にしかない。
 */
function FailedTasksNotice({ labels }: { labels: string[] }) {
  if (labels.length === 0) return null;

  return (
    <ul
      aria-label="失敗したタスク"
      className="flex shrink-0 flex-col gap-1 border-l-[0.25rem] border-l-info bg-info/5 px-2 py-1"
    >
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
  verdictShown,
}: {
  safety: SafetyPayload | undefined;
  onReenergize?: () => void;
  /**
   * 折りたたみ見出しのチップを同じパネルが出しているか。
   *
   * **出しているなら先頭 1 件の文はチップと完全に同じになる** ——
   * `evaluateHealth` は安全機構を最初に見て `${label} ${detail}` をそのまま
   * チップの文言にするため (`lib/healthVerdict.ts`)。真下でもう一度描くと、
   * 同じ文が 2 行離れて 2 度並ぶ。**2 件目以降はチップが言っていない**ので残す。
   */
  verdictShown: boolean;
}) {
  const issues = describeSafetyIssues(safety);
  // 在飛中かはサーバーが配る。押した記憶から組み立てない (拒否された押下まで
  // 「処理中」に見える) し、`unenergized_motors` が消えるのを待つ必要も無い
  const pending = isReenergizePending(safety);
  if (issues.length === 0) return null;

  return (
    <ul className="flex shrink-0 flex-col gap-1 border-l-[0.25rem] border-l-error bg-error/5 px-2 py-1">
      {issues.map((issue, index) => {
        const restated = verdictShown && index === 0;
        return (
          <li key={issue.label} className="flex min-w-0 flex-col">
            {restated ? null : (
              <span className="flex min-w-0 items-center gap-1.5">
                <Icon as={ShieldAlert} className="shrink-0 text-error" />
                <span className="shrink-0 font-medium">{issue.label}</span>
                <span className="min-w-0 truncate font-mono text-base-content/80">
                  {issue.detail}
                </span>
              </span>
            )}
            {/* 状態だけ出しても操縦者は次の一手を選べない。復旧手順まで書く。
                字下げは上のアイコンに揃えるためなので、その行が無い回は付けない */}
            <span
              className={cx("text-[0.85em] text-base-content/70", restated ? null : "pl-[1.4rem]")}
            >
              {issue.hint}
            </span>
            {/* 押せる場所は限定する — この異常が実際に出ていて、かつこの画面に
                コールバックが渡されているとき (操縦者自身の画面) だけ */}
            {issue.kind === "unenergized" && onReenergize ? (
              <Button
                tone="warn"
                className={cx("self-start", restated ? null : "ml-[1.4rem]")}
                onClick={onReenergize}
                disabled={pending}
              >
                {pending ? "処理中…" : "再励磁"}
              </Button>
            ) : null}
          </li>
        );
      })}
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
  onReenergize,
}: SubsystemStatusProps) {
  const verdict = evaluateHealth(health, safety, connected);
  // 内訳を並べる部品は「読めなかった」を表現できない。判定 (上) だけがそれを担う
  const readable = readableHealth(health);
  const riskyBuses = workpieceRiskBuses(health);
  const unconfirmedMotors = firmwareUnconfirmedMotors(safety);
  const latchedAxes = limitLatchedAxes(safety);
  const blindSensors = limitBlindSensors(safety);
  const failedTaskLabels = failedTasks(safety);
  const [manualOpen, setManualOpen] = useState(defaultOpen);
  // **`defaultOpen` は初期値ではなく「今このパネルを開いておくべきか」の宣言。**
  // 呼び出し側 (`RobotControl`) は手動操縦へ切り替わったときに false → true で
  // 渡し直すが、この部品は grid の同じ位置・同じ型のまま残るので**再マウント
  // されない**。`useState` の初期値として受けるだけだと、試合中に手動へ入っても
  // 畳まれたまま、しかもパネルだけが列の全高へ伸びた白い箱になる ——
  // 機体を直接動かしている最中に診断が閉じたままで、手で開かない限り開かない。
  //
  // 宣言が変わった周期だけ追従するので、操縦者が手で畳んだ状態は保たれる
  // (依存が同じ値なら effect は再実行されない)。強制開示 (`forcedOpen`) とは
  // 独立していて、あちらは異常時に操縦者の操作を上書きする別の層。
  useEffect(() => setManualOpen(defaultOpen), [defaultOpen]);
  // 開閉ボタンと開閉対象を結ぶ。aria-expanded だけでは「何が開くのか」が伝わらない
  const detailsId = useId();

  // 異常時は操縦者の開閉操作より優先して開く。畳んだまま見逃させない。
  // ワーク落下の恐れも同格 —— `verdict.tone` はバスが復旧すれば平常に戻るが、
  // こちらは試合中ずっと自分から主張し続けるべき情報なので、判定 (tone) を
  // 変えずにここへ OR で足す。
  //
  // **版番号未確認 (`unconfirmedMotors`) はここに含めない。** `_info` は一度受ければ
  // 二度と None へ戻らないラッチなので、猶予を過ぎても空でないのは大半が
  // 「起動直後のわずかな遅れ」ではなく「その基板は焼き忘れ検出そのものが
  // 効かない」という試合中ずっと変わらない状態になる。ワーク落下のように
  // 試合中の 1 事象ではなく、しかも操縦者は試合中にこれを直せない —— 畳める
  // ままにして、開いたときに見える情報として残す (`defaultOpen` の準備中は開く)
  //
  // **リミット保護のラッチ (`latchedAxes`) は含める。** 判定 (tone) は動かさないが、
  // これは今まさに軸が止まっている状態で、逆向きへ退避するまで解けない。畳んだまま
  // だと操縦者は「指令しても動かない」理由を画面から知る手段が無い。ラッチは自動解除
  // されるので開きっぱなしにもならない (`blindSensors` の方は版番号未確認と同じく
  // 試合中ずっと変わらない状態なので含めない)。
  const forcedOpen =
    verdict.tone === "error" ||
    verdict.tone === "warning" ||
    riskyBuses.length > 0 ||
    latchedAxes.length > 0;
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
          {/* 今まさに軸が止まっている事実なので、恒常的な報告 (版番号・保護無効・
              タスク失敗) より前に置く */}
          <LimitLatchedNotice axes={latchedAxes} />
          <FirmwareUnconfirmedNotice motors={unconfirmedMotors} />
          <LimitBlindNotice sensors={blindSensors} />
          <FailedTasksNotice labels={failedTaskLabels} />
          <SafetyIssues safety={safety} onReenergize={onReenergize} verdictShown={showVerdict} />
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
