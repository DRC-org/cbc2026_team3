import { Check, Copy, RefreshCw } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { Panel } from "@/components/ui/Panel";
import { StatusBadge } from "@/components/ui/StatusBadge";
import type { RobotCommands } from "@/context/RobotContext";
import { cx } from "@/lib/cx";
import type { Malformed, PositionCaptureState, PositionsReloadState } from "@/lib/protocol";
import { MALFORMED } from "@/lib/protocol";
import { formatClock } from "@/lib/time";

interface PositionCapturePanelProps {
  robotKey: string;
  capture: PositionCaptureState | Malformed;
  /** 位置定数 yaml の読み直し。**null なら口が無い台** (ボタンを出さない) */
  reload: PositionsReloadState | Malformed | null;
  blocked: boolean;
  sendOrReport: RobotCommands["sendOrReport"];
}

type CopyState = "idle" | "copied" | "failed";

export function PositionCapturePanel({
  robotKey,
  capture,
  reload,
  blocked,
  sendOrReport,
}: PositionCapturePanelProps) {
  const [copyState, setCopyState] = useState<CopyState>("idle");

  if (capture === MALFORMED) {
    return (
      <Panel legend="位置を控える" className="shrink-0">
        <StatusBadge tone="error">控えの状態 判定不能</StatusBadge>
      </Panel>
    );
  }

  const fragment = capture.yaml;

  const copy = () => {
    if (fragment === null) return;
    navigator.clipboard.writeText(fragment).then(
      () => setCopyState("copied"),
      () => setCopyState("failed"),
    );
  };

  const capturedOf = (axis: string, name: string) =>
    capture.entries.find((entry) => entry.axis === axis && entry.name === name);

  // 軸も位置名もサーバーが配ったものだけを並べる。UI が書き写すと、yaml へ足した名前が
  // 片方の画面にだけ出ない
  const axes = Object.entries(capture.targets);

  return (
    <Panel
      legend="位置を控える"
      className="shrink-0"
      bodyClassName="gap-2 p-2"
      actions={
        <span className="text-[0.85em] text-base-content/60">控え {capture.entries.length}</span>
      }
    >
      {axes.length === 0 ? (
        <p className="text-base-content/70">控えられる軸なし</p>
      ) : (
        axes.map(([axis, names]) => (
          <div key={axis} className="flex flex-wrap items-center gap-1.5">
            <span className="w-28 shrink-0 truncate text-[0.85em] text-base-content/70">
              {axis}
            </span>
            {names.map((name) => {
              const entry = capturedOf(axis, name);
              return (
                <Button
                  key={name}
                  tone={entry ? "ok" : "default"}
                  disabled={blocked}
                  aria-label={`${axis} の現在位置を ${name} として控える`}
                  title={
                    entry
                      ? `${formatClock(entry.captured_at * 1000)} に控えた`
                      : "今いる位置をこの位置名として控える"
                  }
                  onClick={() =>
                    sendOrReport(
                      { type: "position_capture", robot: robotKey, axis, name },
                      "位置を控える",
                    )
                  }
                >
                  {name}
                  {entry ? (
                    <span className="tabular-nums opacity-80">
                      {entry.value.toFixed(1)}
                      {entry.unit}
                    </span>
                  ) : null}
                </Button>
              );
            })}
          </div>
        ))
      )}

      {fragment === null ? null : (
        <div className="flex flex-col gap-1">
          <div className="flex items-center justify-between gap-2">
            <span className="text-[0.85em] text-base-content/60">
              位置定数 yaml へ貼る（書き込むのは人）
            </span>
            <Button onClick={copy} aria-label="yaml 断片をコピー">
              <Icon as={copyState === "copied" ? Check : Copy} />
              {copyState === "copied"
                ? "コピー済み"
                : copyState === "failed"
                  ? "コピー不可"
                  : "コピー"}
            </Button>
          </div>
          <pre
            className={cx(
              "scroll max-h-48 overflow-auto rounded border border-base-300 bg-base-200 p-2",
              "font-mono text-[0.8em] whitespace-pre",
            )}
          >
            {fragment}
          </pre>
        </div>
      )}

      {reload === null ? null : reload === MALFORMED ? (
        <StatusBadge tone="error">読み直しの状態 判定不能</StatusBadge>
      ) : (
        <div className="flex flex-wrap items-center justify-between gap-2">
          <Button
            aria-label="位置定数 yaml を読み直す"
            title="yaml へ貼った値をサーバーへ読ませる（再起動も CAN の入れ直しも要らない）"
            onClick={() =>
              sendOrReport({ type: "positions_reload", robot: robotKey }, "位置定数の読み直し")
            }
          >
            <Icon as={RefreshCw} />
            yaml を読み直す
          </Button>
          <span className="text-[0.85em] text-base-content/60" title={reload.changed.join(", ")}>
            {reload.reloaded_at === null
              ? "貼ったあとに押す"
              : `${formatClock(reload.reloaded_at * 1000)} に読み直し・${
                  reload.changed.length === 0 ? "変化なし" : `${reload.changed.length} 件変化`
                }`}
          </span>
        </div>
      )}
    </Panel>
  );
}
