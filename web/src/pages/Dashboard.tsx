import { Checklist } from "@/components/Checklist";
import { HealthIndicator } from "@/components/HealthIndicator";
import { MatchControl } from "@/components/MatchControl";
import { MotorSummary } from "@/components/MotorSummary";
import { RobotReadiness } from "@/components/RobotReadiness";
import { SequenceProgress } from "@/components/SequenceProgress";
import { useRobot } from "@/context/RobotContext";
import type { ChecklistRole } from "@/hooks/useRobotSocket";
import { cx } from "@/lib/cx";
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
    <div
      className="tui-window"
      style={{ display: "flex", flexDirection: "column", minHeight: 0, overflow: "hidden" }}
    >
      <fieldset
        className="tui-fieldset"
        style={{ display: "flex", flexDirection: "column", flex: 1, minHeight: 0 }}
      >
        <legend>
          <span className="cyan-168-text">SETUP CHECKLIST</span>
        </legend>
        <p style={{ opacity: 0.7, marginBottom: "0.5rem" }}>
          半自動モードでは各操縦者が自分のタブでチェックします (読み取り専用)。
        </p>
        <div
          className="tui-scroll-cyan"
          style={{
            display: "flex",
            flex: 1,
            flexDirection: "column",
            gap: "0.75rem",
            minHeight: 0,
            overflowY: "auto",
          }}
        >
          {OPERATOR_ROLES.map(({ role, label }) => {
            const checklist = matchState.checklists[role];
            const items = checklist?.items ?? [];
            const checked = items.filter((i) => i.checked).length;
            const done = checklist?.completed ?? false;
            return (
              <div key={role}>
                <div style={{ display: "flex", justifyContent: "space-between" }}>
                  <span className={done ? "green-255-text" : "yellow-255-text"}>
                    {done ? "[✓]" : "[ ]"} {label}
                  </span>
                  <span style={{ opacity: 0.8 }}>
                    {checked} / {items.length}
                  </span>
                </div>
                <div style={{ paddingLeft: "1.5rem" }}>
                  {items.map((item) => (
                    <div
                      key={item.id}
                      className={item.checked ? "secondary-text" : "yellow-255-text"}
                      style={{
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                        whiteSpace: "nowrap",
                      }}
                    >
                      {item.checked ? "✓" : "·"} {item.label}
                    </div>
                  ))}
                </div>
              </div>
            );
          })}
        </div>
      </fieldset>
    </div>
  );
}

/** 試合中の Monitor が見る 1 機分のカード。シーケンス進捗を最上段に置く。 */
function RobotCard({ robotKey, label }: { robotKey: string; label: string }) {
  const { states } = useRobot();
  const state = states[robotKey];

  return (
    <div
      className="tui-window"
      style={{ display: "flex", flexDirection: "column", minHeight: 0, overflow: "hidden" }}
    >
      <fieldset
        className="tui-fieldset"
        style={{ display: "flex", flexDirection: "column", flex: 1, minHeight: 0 }}
      >
        <legend>
          <span className="cyan-168-text">{label}</span>
        </legend>
        {state ? (
          <div
            style={{
              display: "flex",
              flexDirection: "column",
              flex: 1,
              minHeight: 0,
              gap: "0.5rem",
            }}
          >
            <fieldset className="tui-fieldset" style={{ flexShrink: 0, marginBottom: 0 }}>
              <legend>SEQUENCE</legend>
              <SequenceProgress
                sequence={state.sequence}
                currentStep={state.current_step}
                stepIndex={state.step_index}
                totalSteps={state.total_steps}
                waitingTrigger={state.waiting_trigger}
              />
            </fieldset>
            <fieldset className="tui-fieldset" style={{ flexShrink: 0, marginBottom: 0 }}>
              <legend>CAN BUS</legend>
              <HealthIndicator variant="bus-only" health={state.health} />
            </fieldset>
            <fieldset
              className="tui-fieldset"
              style={{
                display: "flex",
                flexDirection: "column",
                flex: 1,
                minHeight: 0,
                marginBottom: 0,
              }}
            >
              <legend>MOTORS</legend>
              <MotorSummary motors={state.motors} healthMotors={state.health?.motors} />
            </fieldset>
          </div>
        ) : (
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: "0.5rem",
              padding: "0.5rem 0",
            }}
          >
            <span style={{ opacity: 0.7 }}>データ未受信</span>
            <div className={cx("tui-progress-bar", "inline-block", "valign-middle")}>
              <span className="tui-indeterminate" />
            </div>
          </div>
        )}
      </fieldset>
    </div>
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
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(2, minmax(0, 1fr))",
          // 上段が残り高さを埋め、機体レディネスは常に画面下端に固定される
          gridTemplateRows: "minmax(0, 1fr) auto",
          gap: "0.5rem",
          flex: 1,
          minHeight: 0,
          overflow: "hidden",
          padding: "0.5rem",
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
    <main
      style={{
        display: "flex",
        flex: 1,
        flexDirection: "column",
        gap: "0.5rem",
        minHeight: 0,
        overflow: "hidden",
        padding: "0.5rem",
      }}
    >
      <MatchControl variant="compact" />
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(2, minmax(0, 1fr))",
          gap: "0.5rem",
          flex: 1,
          minHeight: 0,
        }}
      >
        {ROBOTS.map(({ key, label }) => (
          <RobotCard key={key} robotKey={key} label={label} />
        ))}
      </div>
    </main>
  );
}
