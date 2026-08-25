import { Checklist } from "@/components/Checklist";
import { HealthIndicator } from "@/components/HealthIndicator";
import { MatchControl } from "@/components/MatchControl";
import { MotorSummary } from "@/components/MotorSummary";
import { RobotReadiness } from "@/components/RobotReadiness";
import { SequenceProgress } from "@/components/SequenceProgress";
import { Panel } from "@/components/ui/Panel";
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
    <Panel legend="SETUP CHECKLIST">
      <p className="text-fg-dim">
        半自動モードでは各操縦者が自分のタブでチェックします (読み取り専用)。
      </p>
      <div className="panel-body scroll mt-2 gap-3">
        {OPERATOR_ROLES.map(({ role, label }) => {
          const checklist = matchState.checklists[role];
          const items = checklist?.items ?? [];
          const checked = items.filter((i) => i.checked).length;
          const done = checklist?.completed ?? false;
          return (
            <div key={role} className="shrink-0">
              <div className="hsplit">
                <span className={done ? "text-success" : "text-warning"}>
                  {done ? "[✓]" : "[ ]"} {label}
                </span>
                <span className="whitespace-nowrap text-fg-dim">
                  {checked} / {items.length}
                </span>
              </div>
              <div className="pl-6">
                {items.map((item) => (
                  <div
                    key={item.id}
                    className={item.checked ? "truncate text-fg-dim" : "truncate text-warning"}
                  >
                    {item.checked ? "✓" : "·"} {item.label}
                  </div>
                ))}
              </div>
            </div>
          );
        })}
      </div>
    </Panel>
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
      <Panel legend={label}>
        <div className="hstack py-2">
          <span className="whitespace-nowrap text-fg-dim">データ未受信</span>
          <progress className="progress h-[0.9rem] flex-1 bg-base-300" />
        </div>
      </Panel>
    );
  }

  return (
    <Panel legend={label}>
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

      <div className="group min-h-0 flex-1">
        <div className="group-title">MOTORS</div>
        <MotorSummary motors={state.motors} healthMotors={state.health?.motors} />
      </div>
    </Panel>
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
      // 上段が残り高さを埋め、機体レディネスは常に画面下端に固定される
      <main className="page grid grid-cols-2 grid-rows-[minmax(0,1fr)_auto]">
        <MatchControl />
        {matchState.mode === "full_auto" ? (
          <Checklist checklistRole="monitor" title="セッティング指差喚呼 (全自動)" />
        ) : (
          <OperatorChecklistProgress />
        )}
        <div className="col-span-full flex">
          <RobotReadiness />
        </div>
      </main>
    );
  }

  return (
    <main className="page">
      <MatchControl variant="compact" />
      <div className="grid min-h-0 flex-1 grid-cols-2 gap-2">
        {ROBOTS.map(({ key, label }) => (
          <RobotCard key={key} robotKey={key} label={label} />
        ))}
      </div>
    </main>
  );
}
