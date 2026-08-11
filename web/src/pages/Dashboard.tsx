import { useEffect, useState } from "react";

import { Checklist } from "@/components/Checklist";
import { HealthIndicator } from "@/components/HealthIndicator";
import { MatchControl } from "@/components/MatchControl";
import { MotorSummary } from "@/components/MotorSummary";
import { SequenceProgress } from "@/components/SequenceProgress";
import { useRobot } from "@/context/RobotContext";
import type { ChecklistRole, HealthChangeEvent } from "@/hooks/useRobotSocket";
import { cx } from "@/lib/cx";
import { ROBOTS } from "@/lib/robots";

const TOAST_VISIBLE_MS = 5000;

interface ToastState {
  event: HealthChangeEvent;
  id: number;
}

// TUI 風通知: 等幅枠 + [!]/[X]。表示ロジック自体は従来どおり health_change を契機にする。
function HealthToast({ toast, onDismiss }: { toast: ToastState; onDismiss: () => void }) {
  const isCritical = toast.event.level === "critical";
  const textClass = isCritical ? "danger-text" : "warning-text";
  return (
    <div
      className="tui-window"
      style={{
        pointerEvents: "auto",
        width: "20rem",
        maxWidth: "calc(100vw - 2rem)",
      }}
    >
      <fieldset className="tui-fieldset">
        <legend>
          <span className={textClass}>[!] {toast.event.level.toUpperCase()}</span>
        </legend>
        <div
          style={{
            display: "flex",
            alignItems: "flex-start",
            justifyContent: "space-between",
            gap: 8,
          }}
        >
          <div style={{ minWidth: 0 }}>
            <div style={{ opacity: 0.8 }}>{toast.event.robot}</div>
            <div
              style={{
                opacity: 0.8,
                overflow: "hidden",
                textOverflow: "ellipsis",
                whiteSpace: "nowrap",
              }}
            >
              {toast.event.target}
            </div>
            <div className={textClass}>
              {toast.event.from} → {toast.event.to}
            </div>
            {toast.event.message ? (
              <div
                style={{
                  marginTop: 4,
                  opacity: 0.8,
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                  whiteSpace: "nowrap",
                }}
              >
                {toast.event.message}
              </div>
            ) : null}
          </div>
          <button
            type="button"
            onClick={onDismiss}
            aria-label="通知を閉じる"
            style={{
              flexShrink: 0,
              opacity: 0.8,
              background: "transparent",
              border: "none",
              cursor: "pointer",
              color: "inherit",
            }}
          >
            [X]
          </button>
        </div>
      </fieldset>
    </div>
  );
}

const OPERATOR_ROLES: { role: ChecklistRole; label: string }[] = [
  { role: "main_hand", label: "メインハンド 操縦者" },
  { role: "sub_hand", label: "サブハンド 操縦者" },
];

/** 半自動時、Monitor から 2 名の指差喚呼の進み具合を読み取り専用で監視する。 */
function OperatorChecklistProgress() {
  const { matchState } = useRobot();

  return (
    <div className="tui-window">
      <fieldset className="tui-fieldset">
        <legend>
          <span className="cyan-168-text">SETUP CHECKLIST</span>
        </legend>
        <p style={{ opacity: 0.8, marginBottom: "0.5rem" }}>
          半自動モードでは各操縦者が自分のタブでチェックします。
        </p>
        <div style={{ display: "flex", flexDirection: "column", gap: "0.5rem" }}>
          {OPERATOR_ROLES.map(({ role, label }) => {
            const checklist = matchState.checklists[role];
            const items = checklist?.items ?? [];
            const checked = items.filter((i) => i.checked).length;
            const done = checklist?.completed ?? false;
            return (
              <div key={role} style={{ display: "flex", justifyContent: "space-between" }}>
                <span className={done ? "green-255-text" : "yellow-255-text"}>
                  {done ? "[✓]" : "[ ]"} {label}
                </span>
                <span style={{ opacity: 0.8 }}>
                  {checked} / {items.length}
                </span>
              </div>
            );
          })}
        </div>
      </fieldset>
    </div>
  );
}

export function Dashboard() {
  const { states, healthEvents, matchState } = useRobot();
  const [toast, setToast] = useState<ToastState | null>(null);

  useEffect(() => {
    const latest = healthEvents[0];
    if (!latest) return;
    if (latest.level === "info") return;
    setToast({ event: latest, id: latest.receivedAt });
    const timer = setTimeout(() => {
      setToast((prev) => (prev && prev.id === latest.receivedAt ? null : prev));
    }, TOAST_VISIBLE_MS);
    return () => clearTimeout(timer);
  }, [healthEvents]);

  return (
    <>
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "1fr 1fr",
          gap: "0.5rem",
          padding: "0.5rem",
        }}
      >
        <MatchControl />
        {matchState.mode === "full_auto" ? (
          <Checklist checklistRole="monitor" title="セッティング指差喚呼 (全自動)" />
        ) : (
          <OperatorChecklistProgress />
        )}

        {ROBOTS.map(({ key, label }) => {
          const state = states[key];
          return (
            <div key={key} className="tui-window">
              <fieldset className="tui-fieldset">
                <legend>
                  <span className="cyan-168-text">{label}</span>
                </legend>
                {state ? (
                  <div style={{ display: "flex", flexDirection: "column", gap: "0.5rem" }}>
                    <fieldset className="tui-fieldset">
                      <legend>SEQUENCE</legend>
                      <SequenceProgress
                        sequence={state.sequence}
                        currentStep={state.current_step}
                        stepIndex={state.step_index}
                        totalSteps={state.total_steps}
                        waitingTrigger={state.waiting_trigger}
                      />
                    </fieldset>
                    <fieldset className="tui-fieldset">
                      <legend>CAN BUS</legend>
                      <HealthIndicator variant="bus-only" health={state.health} />
                    </fieldset>
                    <fieldset className="tui-fieldset">
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
        })}
      </div>

      {toast ? (
        <div
          style={{
            position: "fixed",
            right: "1rem",
            bottom: "3rem",
            zIndex: 50,
          }}
        >
          <HealthToast toast={toast} onDismiss={() => setToast(null)} />
        </div>
      ) : null}
    </>
  );
}
