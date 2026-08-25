import { useRobot } from "@/context/RobotContext";
import { cx } from "@/lib/cx";
import {
  COURT_LABEL,
  MODE_LABEL,
  PHASE_BAND_CLASS,
  PHASE_LABEL,
  PHASE_TEXT_CLASS,
} from "@/lib/phase";

/**
 * 全画面共通のヘッダー帯。
 *
 * 左端のバー色とフェーズ名の色で「今 機体が動くフェーズか」を示す。
 * 帯全面をフェーズ色で塗ると画面で最も明るい面になってしまうため、
 * 地はグレーに固定し、左端のバーとフェーズ名だけを色で示す。
 * 誤ったコート設定のまま試合に入る事故を防ぐためモード・コートも常時表示する。
 * 緊急停止は最優先操作なので、常に同じ位置・最大サイズでここに置く。
 */
export function AppHeader() {
  const { matchState, onEStop } = useRobot();
  const { mode, court, phase } = matchState;

  return (
    <header
      className={cx(
        "flex shrink-0 items-stretch gap-4 border-b border-l-[0.5rem] border-line bg-raised",
        PHASE_BAND_CLASS[phase],
      )}
    >
      <div className="hstack flex-1 gap-6 px-3 py-1">
        <strong className={cx("whitespace-nowrap", PHASE_TEXT_CLASS[phase])}>
          {PHASE_LABEL[phase]}
        </strong>
        <span className="whitespace-nowrap">
          <span className="text-fg-dim">MODE </span>
          {MODE_LABEL[mode]}
        </span>
        <span className="whitespace-nowrap">
          <span className="text-fg-dim">COURT </span>
          <span className={court === "red" ? "text-error" : "text-info"}>{COURT_LABEL[court]}</span>
        </span>
        <span className="min-w-0 flex-1 truncate text-right text-fg-dim">
          cbc2026_team3_controller
        </span>
      </div>

      <button
        type="button"
        className="flex shrink-0 cursor-pointer items-center gap-2 bg-estop px-6 text-[1.1em] font-bold text-estop-fg hover:bg-[#c8412f]"
        onClick={onEStop}
        aria-label="緊急停止"
      >
        <span className="alert-blink">◆</span> EMG STOP
      </button>
    </header>
  );
}
