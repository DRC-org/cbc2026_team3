import { Fieldset } from "@tsaito18/tuicss-react";

import { useRobot } from "@/context/RobotContext";
import type { RobotState } from "@/hooks/useRobotSocket";
import { ROBOTS, TEMP_WARNING } from "@/lib/robots";

interface Readiness {
  tone: "success" | "warning" | "danger";
  symbol: string;
  label: string;
}

function evaluate(state: RobotState | undefined): Readiness {
  if (!state) return { tone: "danger", symbol: "✗", label: "データ未受信" };

  const health = state.health;
  if (!health) return { tone: "warning", symbol: "○", label: "ヘルス未取得" };

  const downBuses = health.buses.filter((b) => b.state === "down");
  if (downBuses.length > 0) {
    return { tone: "danger", symbol: "✗", label: `CAN 停止 (${downBuses[0].name})` };
  }

  const faultMotors = health.motors.filter((m) => m.state === "fault");
  if (faultMotors.length > 0) {
    return { tone: "danger", symbol: "✗", label: `モータ異常 ${faultMotors.length} 件` };
  }

  const degradedBuses = health.buses.filter((b) => b.state !== "ok");
  const badMotors = health.motors.filter((m) => m.state !== "ok");
  const hotMotors = Object.values(state.motors).filter((m) => m.temp >= TEMP_WARNING);
  const warnCount = degradedBuses.length + badMotors.length + hotMotors.length;
  if (warnCount > 0) {
    return { tone: "warning", symbol: "⚠", label: `要確認 ${warnCount} 件` };
  }

  return { tone: "success", symbol: "✓", label: "READY" };
}

/**
 * セッティングタイム中の機体レディネス表示。
 *
 * 準備中の Monitor は指差喚呼が主役なのでロボットの詳細カードは畳むが、
 * CAN 落ちやモータ異常に気付かないまま試合に入る事故だけは防ぐ必要がある。
 * 「異常があるか」だけに絞った 1 行サマリを残す。詳細は各ハンドのタブで確認する。
 */
export function RobotReadiness() {
  const { states } = useRobot();

  return (
    <Fieldset className="panel" legend="MACHINE READINESS" style={{ flex: 1 }}>
      {ROBOTS.map(({ key, label }) => {
        const state = states[key];
        const readiness = evaluate(state);
        const motorCount = state ? Object.keys(state.motors).length : 0;
        return (
          <div key={key} className="hsplit">
            <span className="nowrap">{label}</span>
            <span className="spacer dim" style={{ textAlign: "right" }}>
              {motorCount > 0 ? `モータ ${motorCount}` : ""}
            </span>
            <span className={`${readiness.tone}-text nowrap`}>
              [{readiness.symbol} {readiness.label}]
            </span>
          </div>
        );
      })}
    </Fieldset>
  );
}
