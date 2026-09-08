import { OctagonX } from "lucide-react";

import { Clock } from "@/components/shell/Clock";
import { TabBar } from "@/components/shell/TabBar";
import { Icon } from "@/components/ui/Icon";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { useRobotCommands, useRobotStatus } from "@/context/RobotContext";
import { cx } from "@/lib/cx";
import { COURT_LABEL, COURT_TONE, PHASE_BAND_CLASS, PHASE_LABEL, PHASE_TONE } from "@/lib/phase";
import { TONE_STATUS_CLASS } from "@/lib/tone";

/** 帯の横幅は限られるので host:port だけ出す（全体は title 属性で見せる） */
function wsHostLabel(url: string): string {
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
}

/**
 * 全画面共通のヘッダー帯。画面唯一の常設帯として、機体の設定・画面切替・接続・
 * 緊急停止を 1 段に収める。
 *
 * タブを別の帯に分けると縦を 2 段消費し、1366x768 級のノート PC ではその 1 段が操作領域を
 * 目に見えて削る。同じ理由で折り返さない（`flex-wrap` は狭い画面で 2 段に折れる）。
 * 詰まったときに削ってよいのはタブ帯だけなので、そこだけが縮み、他は `shrink-0`。
 *
 * **並びは押せるかどうかで決める**（docs/invariants.md 「EMG STOP の周囲に押下可能な
 * 要素を置かない」）—— 押せるもの（タブ帯・接続表示）を EMG STOP から遠い側へ、
 * 押せないもの（フェーズ・コート・時計）を EMG STOP 側へ寄せる。**EMG STOP 手前の
 * `ml-6` は装飾ではなく緩衝で、詰めてはならない。** 群は対で保つ —— フェーズとコートは
 * 隣同士、接続と時刻も対（離すと試合設定を 2 回に分けて読むことになる）。
 *
 * 左端のバー色とフェーズチップで「今 機体が動くフェーズか」を示す。帯全面をフェーズ色で
 * 塗ると画面で最も明るい面になるので、地は白に固定する。
 *
 * キー凡例はここに置かない（キーが効く場所 —— タブ自身と START / NEXT ボタン —— が持つ）。
 */
export function AppHeader() {
  const { connected, matchState, wsUrl } = useRobotStatus();
  const { onEStop, openWsSettings } = useRobotCommands();
  const { court, phase } = matchState;

  return (
    <header
      className={cx(
        "flex shrink-0 items-stretch border-b border-l-[0.4rem] border-base-300 bg-base-100",
        PHASE_BAND_CLASS[phase],
      )}
    >
      <div className="flex min-w-0 flex-1 items-center gap-x-3 px-2 py-1">
        {/* 帯が詰まったときに削るのはここだけ。他を縮めると設定と停止が読めなくなる */}
        <div className="min-w-0 overflow-hidden">
          <TabBar />
        </div>

        {/* 右群は EMG STOP へ近い順に「押せない」度合いを上げる。押せる接続表示を
            先頭へ置き、停止ボタンの直左には読むだけの要素しか並ばないようにする */}
        <div className="ml-auto flex shrink-0 items-center gap-3">
          <div className="flex shrink-0 items-center gap-3 text-[0.82em] text-base-content/70">
            {/* 接続表示そのものを接続先設定の入口にする。繋がらない時に最初に見る場所なので */}
            <button
              type="button"
              onClick={openWsSettings}
              className="flex cursor-pointer items-center gap-1.5 hover:text-base-content"
              title={`接続先: ${wsUrl}（クリックで変更）`}
            >
              <span
                className={cx(TONE_STATUS_CLASS[connected ? "success" : "error"], "status-sm")}
              />
              {connected ? "Connected" : "Disconnected"}
              <span className="font-mono">{wsHostLabel(wsUrl)}</span>
            </button>

            <Clock />
          </div>

          <div className="flex shrink-0 items-center gap-1.5">
            <StatusBadge tone={PHASE_TONE[phase]}>{PHASE_LABEL[phase]}</StatusBadge>
            <StatusBadge tone={COURT_TONE[court]}>{COURT_LABEL[court]}</StatusBadge>
          </div>
        </div>
      </div>

      <button
        type="button"
        className="ml-6 flex shrink-0 cursor-pointer items-center gap-2 bg-estop px-6 text-[1.1em] font-bold text-estop-fg hover:bg-[#a82418] focus-visible:outline-2 focus-visible:outline-offset-[-4px] focus-visible:outline-estop-fg"
        onClick={onEStop}
        aria-label="緊急停止"
      >
        <Icon as={OctagonX} className="text-[1.25em]" />
        EMG STOP
      </button>
    </header>
  );
}
