import { useRobot } from "@/context/RobotContext";
import {
  COURT_LABEL,
  MODE_LABEL,
  PHASE_BAND_CLASS,
  PHASE_LABEL,
  PHASE_TEXT_CLASS,
} from "@/lib/phase";

/**
 * 全タブ共通のヘッダー帯。
 *
 * 左端のバー色とフェーズ名の色で「今 機体が動くフェーズか」を示す。
 * 誤ったコート設定のまま試合に入る事故を防ぐためモード・コートも常時表示する。
 * 緊急停止は最優先操作なので、常に同じ位置・最大サイズでここに置く。
 */
export function AppHeader() {
  const { matchState, onEStop } = useRobot();
  const { mode, court, phase } = matchState;

  return (
    <header className={PHASE_BAND_CLASS[phase]}>
      <div className="hstack" style={{ flex: 1, gap: "1.5rem", padding: "0.25rem 0.75rem" }}>
        <strong className={`nowrap ${PHASE_TEXT_CLASS[phase]}`}>{PHASE_LABEL[phase]}</strong>
        <span className="nowrap">
          <span className="dim">MODE </span>
          {MODE_LABEL[mode]}
        </span>
        <span className="nowrap">
          <span className="dim">COURT </span>
          <span className={court === "red" ? "danger-text" : "info-text"}>
            {COURT_LABEL[court]}
          </span>
        </span>
        <span className="spacer ellipsis dim" style={{ textAlign: "right" }}>
          cbc2026_team3_controller
        </span>
      </div>

      <button type="button" className="estop-button" onClick={onEStop} aria-label="緊急停止">
        <span className="alert-blink">◆</span> EMG STOP
      </button>
    </header>
  );
}
