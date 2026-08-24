import { Fieldset, ProgressBar } from "@tsaito18/tuicss-react";

import { Checklist } from "@/components/Checklist";
import { HealthIndicator } from "@/components/HealthIndicator";
import { MatchControl } from "@/components/MatchControl";
import { MotorSummary } from "@/components/MotorSummary";
import { RobotReadiness } from "@/components/RobotReadiness";
import { SequenceProgress } from "@/components/SequenceProgress";
import { useRobot } from "@/context/RobotContext";
import type { ChecklistRole } from "@/hooks/useRobotSocket";
import { isSetupPhase } from "@/lib/phase";
import { ROBOTS } from "@/lib/robots";

const OPERATOR_ROLES: { role: ChecklistRole; label: string }[] = [
  { role: "main_hand", label: "メインハンド 操縦者" },
  { role: "sub_hand", label: "サブハンド 操縦者" },
];

/**
 * 半自動時、Monitor から 2 名の指差喚呼の進み具合を読み取り専用で監視する。
 *
 * 完了件数だけでは「誰の何が残っているか」が分からず、試合開始が遅れる原因を
 * 探すのに操縦者へ聞きにいく必要があった。未完了の項目名まで出す。
 */
function OperatorChecklistProgress() {
  const { matchState } = useRobot();

  return (
    <Fieldset className="panel" legend="SETUP CHECKLIST">
      <p className="dim">半自動モードでは各操縦者が自分のタブでチェックします (読み取り専用)。</p>
      <div className="panel-body scroll" style={{ gap: "0.75rem", marginTop: "0.5rem" }}>
        {OPERATOR_ROLES.map(({ role, label }) => {
          const checklist = matchState.checklists[role];
          const items = checklist?.items ?? [];
          const checked = items.filter((i) => i.checked).length;
          const done = checklist?.completed ?? false;
          return (
            <div key={role} className="no-shrink">
              <div className="hsplit">
                <span className={done ? "success-text" : "warning-text"}>
                  {done ? "[✓]" : "[ ]"} {label}
                </span>
                <span className="dim nowrap">
                  {checked} / {items.length}
                </span>
              </div>
              <div style={{ paddingLeft: "1.5rem" }}>
                {items.map((item) => (
                  <div
                    key={item.id}
                    className={item.checked ? "secondary-text ellipsis" : "warning-text ellipsis"}
                  >
                    {item.checked ? "✓" : "·"} {item.label}
                  </div>
                ))}
              </div>
            </div>
          );
        })}
      </div>
    </Fieldset>
  );
}

/**
 * 試合中の Monitor が見る 1 機分のカード。シーケンス進捗を最上段に置く。
 *
 * 枠はカード外周の 1 本だけ。内訳 (SEQUENCE / CAN BUS / MOTORS) は
 * 枠を入れ子にせず罫線と小見出しで区切る。
 */
function RobotCard({ robotKey, label }: { robotKey: string; label: string }) {
  const { states } = useRobot();
  const state = states[robotKey];

  if (!state) {
    return (
      <Fieldset className="panel" legend={label}>
        <div className="hstack" style={{ padding: "0.5rem 0" }}>
          <span className="dim nowrap">データ未受信</span>
          <ProgressBar indeterminate style={{ flex: 1 }} />
        </div>
      </Fieldset>
    );
  }

  return (
    <Fieldset className="panel" legend={label}>
      <div className="group">
        <div className="group-title">SEQUENCE</div>
        <SequenceProgress
          sequence={state.sequence}
          currentStep={state.current_step}
          stepIndex={state.step_index}
          totalSteps={state.total_steps}
          waitingTrigger={state.waiting_trigger}
        />
      </div>

      <div className="group">
        <div className="group-title">CAN BUS</div>
        <HealthIndicator variant="bus-only" health={state.health} />
      </div>

      <div className="group group-fill">
        <div className="group-title">MOTORS</div>
        <MotorSummary motors={state.motors} healthMotors={state.health?.motors} />
      </div>
    </Fieldset>
  );
}

/**
 * Monitor タブ。フェーズによって役割が変わるため、レイアウトごと切り替える。
 *
 * - セッティングタイム: 試合設定と指差喚呼が主役。ロボットの詳細は畳み、異常有無のみ残す
 * - 試合中 / 試合終了: 両機の状態監視が主役。試合制御は終了導線だけの 1 行に縮退させる
 */
export function Dashboard() {
  const { matchState } = useRobot();

  if (isSetupPhase(matchState.phase)) {
    return (
      <main
        className="page"
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(2, minmax(0, 1fr))",
          // 上段が残り高さを埋め、機体レディネスは常に画面下端に固定される
          gridTemplateRows: "minmax(0, 1fr) auto",
        }}
      >
        <MatchControl />
        {matchState.mode === "full_auto" ? (
          <Checklist checklistRole="monitor" title="セッティング指差喚呼 (全自動)" />
        ) : (
          <OperatorChecklistProgress />
        )}
        <div style={{ display: "flex", gridColumn: "1 / -1" }}>
          <RobotReadiness />
        </div>
      </main>
    );
  }

  return (
    <main className="page">
      <MatchControl variant="compact" />
      <div className="grid-2 fill">
        {ROBOTS.map(({ key, label }) => (
          <RobotCard key={key} robotKey={key} label={label} />
        ))}
      </div>
    </main>
  );
}
