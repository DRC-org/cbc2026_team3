import { OctagonX } from "lucide-react";

import { TabBar } from "@/components/TabBar";
import { Icon } from "@/components/ui/Icon";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { useRobot } from "@/context/RobotContext";
import { cx } from "@/lib/cx";
import {
  COURT_LABEL,
  COURT_TONE,
  MODE_LABEL,
  PHASE_BAND_CLASS,
  PHASE_LABEL,
  PHASE_TONE,
} from "@/lib/phase";

/**
 * 全画面共通のヘッダー帯。フェーズ表示・タブ・試合設定・緊急停止を 1 段に収める。
 *
 * タブを別の帯に分けると縦を 2 段消費する。1366x768 級のノート PC では
 * その 1 段が操作領域を目に見えて削るため、同じ帯へ畳んでいる。
 *
 * 左端のバー色とフェーズチップで「今 機体が動くフェーズか」を示す。
 * 帯全面をフェーズ色で塗ると画面で最も明るい面になってしまうため、地は白に固定する。
 * 誤ったコート設定のまま試合に入る事故を防ぐためモード・コートも常時表示する。
 * 緊急停止は最優先操作なので、常に同じ位置・最大サイズでここに置く。
 */
export function AppHeader() {
  const { matchState, onEStop } = useRobot();
  const { mode, court, phase } = matchState;

  return (
    <header
      className={cx(
        "flex shrink-0 items-stretch border-b border-l-[0.4rem] border-base-300 bg-base-100",
        PHASE_BAND_CLASS[phase],
      )}
    >
      <div className="flex min-w-0 flex-1 flex-wrap items-center gap-x-3 gap-y-1 px-2 py-1">
        <StatusBadge tone={PHASE_TONE[phase]}>{PHASE_LABEL[phase]}</StatusBadge>
        <TabBar />
        <div className="ml-auto flex shrink-0 items-center gap-2">
          <span className="badge badge-ghost border-base-300 badge-sm">{MODE_LABEL[mode]}</span>
          <StatusBadge tone={COURT_TONE[court]}>{COURT_LABEL[court]}</StatusBadge>
        </div>
      </div>

      <button
        type="button"
        className="flex shrink-0 cursor-pointer items-center gap-2 bg-estop px-6 text-[1.1em] font-bold text-estop-fg hover:bg-[#a82418] focus-visible:outline-2 focus-visible:outline-offset-[-4px] focus-visible:outline-estop-fg"
        onClick={onEStop}
        aria-label="緊急停止"
      >
        <Icon as={OctagonX} className="alert-blink text-[1.25em]" />
        EMG STOP
      </button>
    </header>
  );
}
