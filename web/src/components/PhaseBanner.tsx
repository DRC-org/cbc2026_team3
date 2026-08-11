import { useRobot } from "@/context/RobotContext";
import type { MatchCourt, MatchMode, MatchPhase } from "@/hooks/useRobotSocket";

const MODE_LABEL: Record<MatchMode, string> = {
  semi_auto: "半自動",
  full_auto: "全自動",
};

const COURT_LABEL: Record<MatchCourt, string> = {
  red: "赤コート",
  blue: "青コート",
};

const PHASE_LABEL: Record<MatchPhase, string> = {
  setup: "セッティングタイム",
  ready: "試合開始待ち",
  match: "試合中",
  finished: "試合終了",
};

const PHASE_CLASS: Record<MatchPhase, string> = {
  setup: "yellow-255-text",
  ready: "cyan-255-text",
  match: "green-255-text",
  finished: "white-255-text",
};

/**
 * モード・コート・フェーズの常時表示。
 * 誤ったコート設定のまま試合に入る事故を防ぐため、全タブ共通のヘッダーに置く。
 */
export function PhaseBanner() {
  const { matchState } = useRobot();
  const { mode, court, phase } = matchState;

  return (
    <span style={{ display: "inline-flex", gap: "1rem" }}>
      <span>
        MODE <span className="cyan-255-text">{MODE_LABEL[mode]}</span>
      </span>
      <span>
        COURT{" "}
        <span className={court === "red" ? "red-255-text" : "blue-255-text"}>
          {COURT_LABEL[court]}
        </span>
      </span>
      <span>
        PHASE <span className={PHASE_CLASS[phase]}>{PHASE_LABEL[phase]}</span>
      </span>
    </span>
  );
}
