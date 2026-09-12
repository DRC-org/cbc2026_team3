import { Check, ChevronDown, ChevronRight, ListMinus, Square, TriangleAlert } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";

import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { MalformedNotice } from "@/components/ui/MalformedNotice";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { useRobotStatus } from "@/context/RobotContext";
import { useMotorCheck } from "@/hooks/useMotorCheck";
import { cx } from "@/lib/cx";
import { motorCheckStatus } from "@/lib/motorCheckStatus";
import { MALFORMED } from "@/lib/protocol";
import { TONE_PROGRESS_CLASS } from "@/lib/tone";

export function MotorCheckPanel() {
  const { connected } = useRobotStatus();
  const { state, abort } = useMotorCheck();

  const { outcome, completedSteps: done, failureReason } = motorCheckStatus(state, connected);
  const total = state.total_steps;
  const percent = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0;

  const [manualOpen, setManualOpen] = useState(false);
  const detailsId = useId();
  const forcedOpen = outcome === "running" || outcome === "failed";
  const open = forcedOpen || manualOpen;

  const panelRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (outcome === "running" || outcome === "failed") {
      panelRef.current?.scrollIntoView({ block: "start", behavior: "smooth" });
    }
  }, [outcome]);

  return (
    <div ref={panelRef} className="flex flex-col">
      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={() => setManualOpen(!open)}
          aria-expanded={open}
          aria-controls={detailsId}
          className="flex min-w-0 flex-1 cursor-pointer items-center gap-1.5 py-0.5 text-left text-base-content/70 hover:text-base-content"
        >
          <Icon as={open ? ChevronDown : ChevronRight} className="text-base-content/60" />
          <span className="min-w-0 truncate">手順と結果</span>
        </button>
        {state.running ? (
          <Button tone="danger" onClick={abort}>
            <Icon as={Square} />
            中断
          </Button>
        ) : null}
      </div>

      {open ? (
        <div id={detailsId} className="flex flex-col gap-2 pt-1">
          {state.running ? (
            <div className="flex flex-col gap-1">
              <div className="flex items-center justify-between gap-2">
                <span className="font-mono text-base-content/70 tabular-nums">
                  {done} / {total}
                </span>
                <span className="min-w-0 truncate text-info">{state.current_step ?? "—"}</span>
              </div>
              <progress
                className={cx(
                  "progress h-[0.7rem] w-full border border-base-300 bg-base-200",
                  TONE_PROGRESS_CLASS.info,
                )}
                value={percent}
                max={100}
              />
            </div>
          ) : null}

          {failureReason ? (
            <div className="text-error">
              <p className="flex items-center gap-1.5 font-medium">
                <Icon as={TriangleAlert} />
                動作確認は完了していません
              </p>
              <p className="mt-1">{failureReason}</p>
            </div>
          ) : null}

          {state.excluded_steps === MALFORMED ? (
            <MalformedNotice
              subject="除外ステップ"
              detail="ステップ一覧が全てを表しているとは限りません (配信の形が読めていません)。"
            />
          ) : state.excluded_steps.length > 0 ? (
            <div className="rounded-sm border border-warning/40 bg-warning/10 px-3 py-2">
              <p className="flex items-center gap-1.5 font-medium text-warning">
                <Icon as={ListMinus} />
                この構成に無い軸のステップを {state.excluded_steps.length} 件除外しています
              </p>
              <ul className="mt-1 flex flex-col gap-0.5">
                {state.excluded_steps.map((excluded) => (
                  <li key={excluded.step} className="flex flex-wrap items-baseline gap-x-2">
                    <span className="text-base-content/80">{excluded.step}</span>
                    <span className="font-mono text-[0.85em] text-base-content/60">
                      軸が無い: {excluded.missing_axes.join(", ")}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          ) : null}

          {state.steps === MALFORMED ? (
            <MalformedNotice
              subject="ステップ一覧"
              detail="配信の形が読めていません。動作確認の結果は判断材料になりません。"
            />
          ) : state.steps.length === 0 ? (
            <p className="px-1 py-1 text-base-content/70">
              {state.available
                ? "動作確認のステップが読み込まれていません。"
                : "この構成では動作確認を実行できません (位置定数が揃っていません)。"}
            </p>
          ) : (
            <ol className="flex flex-col">
              {state.steps.map((step) => {
                const isCurrent = state.running && step.index === state.step_index;
                const isDone = step.index < done;
                return (
                  <li
                    key={step.index}
                    className={cx(
                      "flex items-center gap-2 border-l-2 border-transparent px-2 py-[0.35rem]",
                      isCurrent && "border-l-info bg-base-200 font-medium",
                      isDone && "text-base-content/45",
                    )}
                  >
                    <span className="w-6 shrink-0 text-right font-mono text-base-content/50 tabular-nums">
                      {step.index + 1}
                    </span>
                    <span className="min-w-0 flex-1 truncate">{step.label}</span>
                    {isCurrent ? (
                      <StatusBadge tone="info">実行中</StatusBadge>
                    ) : isDone ? (
                      <Icon as={Check} className="shrink-0 text-success" />
                    ) : null}
                  </li>
                );
              })}
            </ol>
          )}
        </div>
      ) : null}
    </div>
  );
}
