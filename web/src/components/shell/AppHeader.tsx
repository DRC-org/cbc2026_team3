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
 * タブを別の帯に分けると縦を 2 段消費する。1366x768 級のノート PC では
 * その 1 段が操作領域を目に見えて削るため、同じ帯へ畳んでいる。同じ理由で
 * 折り返さない（`flex-wrap` を付けると狭い画面で 2 段に折れ、畳んだ意味が消える）。
 * 詰まったときに削ってよいのはタブ帯だけなので、そこだけが縮み、他は `shrink-0`。
 *
 * **EMG STOP の周囲に押下可能な要素を置かない。** 誤爆の向きは「隣のボタンを
 * 押そうとして EMG STOP を踏む」で、試合中にこれが起きると走っているシーケンスが
 * その場で止まる。そこで並びは押せるかどうかで決める —— 押せるもの（タブ帯・接続表示）を
 * EMG STOP から遠い側へ、押せないもの（フェーズ・コート・時計）を EMG STOP 側へ寄せる。
 * **タブ帯が最左**なのは、画面の隅がポインタで最も当てやすい位置であると同時に、
 * EMG STOP から最も遠いため。**EMG STOP 手前の `ml-6` は装飾ではなく緩衝で、
 * 詰めてはならない**（詰めると右群の右端と停止ボタンが地続きになり、外した 1 回が
 * そのまま停止になる）。
 *
 * 群そのものは従来どおり保つ —— フェーズとコートは対の情報なので隣に置き
 * （帯の両端へ離すと試合設定を 2 回に分けて読むことになる）、接続と時刻も対で置く。
 *
 * 左端のバー色とフェーズチップで「今 機体が動くフェーズか」を示す。
 * 帯全面をフェーズ色で塗ると画面で最も明るい面になってしまうため、地は白に固定する。
 * 誤ったコート設定のまま試合に入る事故を防ぐためコートも常時表示する。
 * 緊急停止は最優先操作なので、常に同じ位置・最大サイズでここに置く。
 *
 * キー凡例はここに置かない。数字キーはタブ自身が、Space は START / NEXT ボタン自身が
 * 持っている。キーが効く場所から離れた所で同じことを繰り返す面積は無い。
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
