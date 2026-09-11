import { onOffPair } from "@/components/operator/OnOffPadGroup";
import { PadToggle } from "@/components/operator/PadToggle";
import { Panel } from "@/components/ui/Panel";
import { StatusBadge } from "@/components/ui/StatusBadge";
import type { RobotCommands } from "@/context/RobotContext";
import type { Malformed, ManualState, SuctionState } from "@/lib/protocol";
import { MALFORMED } from "@/lib/protocol";

interface SuctionPadPanelProps {
  robotKey: string;
  suction: SuctionState | Malformed;
  manual: ManualState;
  blockedReason: string | null;
  sendOrReport: RobotCommands["sendOrReport"];
}

export function SuctionPadPanel({
  robotKey,
  suction,
  manual,
  blockedReason,
  sendOrReport,
}: SuctionPadPanelProps) {
  if (suction === MALFORMED) {
    return (
      <Panel legend="吸着パッド" className="shrink-0">
        <StatusBadge tone="error">パッド状態 判定不能</StatusBadge>
      </Panel>
    );
  }

  // 手動では同じ 6 個が「宣言」と「今すぐ開閉」で 2 段に並び、どちらが機体を動かすのか
  // 画面から読めなかった。手動ではこの面が開閉そのものを持つ
  const inManual = manual.mode === "manual";

  // ワークは棚の手前から順に取るので飛び飛びのパッドを選ぶ場面が無く、押す 1 回で個数が決まる
  const selectUpTo = (index: number) => {
    // 送るのは差分ではなく「使う弁の全集合」。2 台の UI が別々に押しても最後に届いた形が正になる
    const next = suction.pads.slice(0, index + 1).map((pad) => pad.axis);
    sendOrReport({ type: "suction_pads_set", robot: robotKey, pads: next }, "吸着パッドの選択");
  };

  const move = (axis: string, position: string) =>
    sendOrReport({ type: "manual_move", robot: robotKey, axis, position }, "プリセット移動");

  const declarePads = suction.pads.map((pad, index) => ({
    key: pad.axis,
    label: pad.label,
    on: pad.enabled,
    unknown: false,
    disabled: blockedReason !== null,
    ariaLabel:
      index === 0
        ? `パッド ${pad.label} を使う`
        : `パッド ${suction.pads[0].label}〜${pad.label} を使う`,
    onClick: () => selectUpTo(index),
  }));

  const openPads = suction.pads.map((pad) => {
    const axis = manual.axes.find((candidate) => candidate.name === pad.axis);
    // 軸も位置名も配信から引く。無いものを推測で埋めるとその弁だけ別の弁が開く
    const pair = axis === undefined ? null : onOffPair(axis);
    const on = axis !== undefined && axis.target !== null && axis.target !== 0;
    return {
      key: pad.axis,
      label: pad.label,
      on,
      unknown: axis === undefined || axis.target === null,
      disabled: blockedReason !== null || pair === null,
      ariaLabel: `パッド ${pad.label} を${on ? "閉じる" : "開く"}`,
      onClick: () => {
        if (pair !== null) move(pad.axis, on ? pair.off : pair.on);
      },
    };
  });

  const pads = inManual ? openPads : declarePads;
  const fillFromNote =
    suction.fill_from === "left"
      ? "左端から"
      : suction.fill_from === "right"
        ? "右端から"
        : "コート未確定";
  const onCount = pads.filter((pad) => pad.on).length;

  return (
    <Panel
      legend="吸着パッド"
      className="shrink-0"
      bodyClassName="p-0"
      actions={
        blockedReason ? (
          <StatusBadge tone="error">{blockedReason}</StatusBadge>
        ) : inManual ? (
          <span className="text-[0.85em] text-base-content/60">
            開 {onCount}/{suction.pads.length}
          </span>
        ) : (
          <span className="flex items-center gap-1.5 text-[0.85em] text-base-content/60">
            {fillFromNote}
            {onCount === 0 ? (
              <StatusBadge tone="warning">未選択</StatusBadge>
            ) : (
              <span>
                使用 {onCount}/{suction.pads.length}
              </span>
            )}
          </span>
        )
      }
    >
      <div
        className="flex flex-wrap gap-2 p-2"
        role="group"
        aria-label={inManual ? "吸着パッドの開閉" : "吸着に使うパッド"}
      >
        {pads.map((pad) => (
          <PadToggle
            key={pad.key}
            label={pad.label}
            on={pad.on}
            unknown={pad.unknown}
            disabled={pad.disabled}
            ariaLabel={pad.ariaLabel}
            onClick={pad.onClick}
          />
        ))}
      </div>
    </Panel>
  );
}
