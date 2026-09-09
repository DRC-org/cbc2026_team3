import { Button } from "@/components/ui/Button";
import { Panel } from "@/components/ui/Panel";
import { StatusBadge } from "@/components/ui/StatusBadge";
import type { RobotCommands } from "@/context/RobotContext";
import { cx } from "@/lib/cx";
import type { Malformed, SuctionState } from "@/lib/protocol";
import { MALFORMED } from "@/lib/protocol";

interface SuctionPadPanelProps {
  robotKey: string;
  suction: SuctionState | Malformed;
  blockedReason: string | null;
  sendOrReport: RobotCommands["sendOrReport"];
}

const ENABLED_CLASS = "border-success bg-success text-success-content hover:bg-success/85";

export function SuctionPadPanel({
  robotKey,
  suction,
  blockedReason,
  sendOrReport,
}: SuctionPadPanelProps) {
  if (suction === MALFORMED) {
    return (
      <Panel legend="吸着パッド" className="shrink-0">
        <StatusBadge tone="error">吸着パッドの状態を読み取れませんでした</StatusBadge>
      </Panel>
    );
  }

  const enabled = suction.pads.filter((pad) => pad.enabled);

  const toggle = (axis: string) => {
    // 送るのは差分ではなく「使う弁の全集合」。2 台の UI が別々に押しても最後に届いた形が正になる
    const next = suction.pads
      .filter((pad) => (pad.axis === axis ? !pad.enabled : pad.enabled))
      .map((pad) => pad.axis);
    sendOrReport({ type: "suction_pads_set", robot: robotKey, pads: next }, "吸着パッドの選択");
  };

  return (
    <Panel
      legend="吸着パッド"
      className="shrink-0"
      bodyClassName="p-0"
      actions={
        blockedReason ? (
          <StatusBadge tone="error">{blockedReason}</StatusBadge>
        ) : enabled.length === 0 ? (
          <StatusBadge tone="warning">未選択 — 吸着ステップは拒否されます</StatusBadge>
        ) : (
          <span className="text-[0.85em] text-base-content/60">
            使用 {enabled.length}/{suction.pads.length}
          </span>
        )
      }
    >
      <div className="flex flex-wrap gap-1 p-2" role="group" aria-label="吸着に使うパッド">
        {suction.pads.map((pad) => (
          <Button
            key={pad.axis}
            className={cx("min-w-[3.5rem] font-mono", pad.enabled && ENABLED_CLASS)}
            disabled={blockedReason !== null}
            aria-pressed={pad.enabled}
            aria-label={`パッド ${pad.label} を${pad.enabled ? "使わない" : "使う"}`}
            onClick={() => toggle(pad.axis)}
          >
            {pad.label}
          </Button>
        ))}
      </div>

      <p className="shrink-0 border-t border-base-300 px-2 py-1 text-[0.8em] text-base-content/55">
        ON のパッドだけを吸着に使います。次の「ワーク吸着」ステップから効きます（実行中の吸着には
        反映されません）。
      </p>
    </Panel>
  );
}
