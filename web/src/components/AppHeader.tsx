import { useRobot } from "@/context/RobotContext";
import { COURT_LABEL, MODE_LABEL, PHASE_BAND_CLASS, PHASE_LABEL } from "@/lib/phase";

/**
 * 全タブ共通のヘッダー帯。
 *
 * 帯の地色をフェーズ色にしているのは、視線を落とさずに「今 機体が動くフェーズか」を
 * 判別させるため。誤ったコート設定のまま試合に入る事故を防ぐためモード・コートも常時表示する。
 * 緊急停止は最優先操作なので、常に同じ位置・最大サイズでここに置く。
 */
export function AppHeader() {
  const { matchState, onEStop } = useRobot();
  const { mode, court, phase } = matchState;

  return (
    <header
      className={PHASE_BAND_CLASS[phase]}
      style={{
        display: "flex",
        flexShrink: 0,
        alignItems: "stretch",
        gap: "1rem",
      }}
    >
      <div
        style={{
          display: "flex",
          flex: 1,
          minWidth: 0,
          alignItems: "center",
          gap: "1.5rem",
          padding: "0.25rem 0.75rem",
        }}
      >
        <strong style={{ whiteSpace: "nowrap" }}>{PHASE_LABEL[phase]}</strong>
        <span style={{ whiteSpace: "nowrap" }}>
          <span style={{ opacity: 0.7 }}>MODE </span>
          {MODE_LABEL[mode]}
        </span>
        <span style={{ whiteSpace: "nowrap" }}>
          <span style={{ opacity: 0.7 }}>COURT </span>
          <span className={court === "red" ? "red-255-text" : "blue-255-text"}>
            {COURT_LABEL[court]}
          </span>
        </span>
        <span
          style={{
            minWidth: 0,
            flex: 1,
            textAlign: "right",
            opacity: 0.6,
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
          }}
        >
          cbc2026_team3_controller
        </span>
      </div>

      <button
        type="button"
        className="red-255 white-255-text"
        onClick={onEStop}
        aria-label="緊急停止"
        style={{
          display: "flex",
          flexShrink: 0,
          alignItems: "center",
          gap: "0.5rem",
          padding: "0 1.5rem",
          border: "none",
          cursor: "pointer",
          fontSize: "1.1em",
          fontWeight: "bold",
        }}
      >
        <span className="yellow-255-text">◆</span> EMG STOP
      </button>
    </header>
  );
}
