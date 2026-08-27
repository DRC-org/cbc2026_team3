import { Play, Square, TriangleAlert } from "lucide-react";

import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { Modal } from "@/components/ui/Modal";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { useMotorCheck } from "@/hooks/useMotorCheck";
import { cx } from "@/lib/cx";
import type { MotorCheckOverall, MotorCheckRecord, MotorCheckResult } from "@/lib/protocol";
import type { Tone } from "@/lib/tone";
import { TONE_PROGRESS_CLASS } from "@/lib/tone";

interface MotorCheckPanelProps {
  robotName: string;
  isOpen: boolean;
  onOpenChange: (open: boolean) => void;
}

const RESULT_STYLES: Record<MotorCheckResult, { tone: Tone; label: string }> = {
  pending: { tone: "neutral", label: "待機中" },
  running: { tone: "info", label: "確認中" },
  passed: { tone: "success", label: "合格" },
  failed: { tone: "error", label: "失敗" },
  timeout: { tone: "warning", label: "タイムアウト" },
  skipped: { tone: "neutral", label: "中断" },
};

const OVERALL_STYLES: Record<MotorCheckOverall, { tone: Tone; label: string }> = {
  running: { tone: "info", label: "実行中" },
  ok: { tone: "success", label: "全モータ合格" },
  partial: { tone: "warning", label: "一部失敗" },
  failed: { tone: "error", label: "失敗" },
};

function formatNumber(value: number): string {
  if (Number.isInteger(value)) return value.toFixed(0);
  if (Math.abs(value) >= 100) return value.toFixed(1);
  return value.toFixed(2);
}

function describeRecord(record: MotorCheckRecord): string {
  switch (record.result) {
    case "passed": {
      // 到達位置を判定しない項目 (グリッパの開閉等) は expected を持たない。
      // 生値をそのまま埋め込むと画面に "期待 null" と出る
      const parts = [
        record.expected === null ? null : `期待 ${formatNumber(record.expected)}`,
        record.observed === null ? null : `観測 ${formatNumber(record.observed)}`,
      ].filter((part) => part !== null);
      return parts.length > 0 ? parts.join(" → ") : "合格";
    }
    case "failed":
      return record.detail ?? "失敗";
    case "timeout":
      return record.detail ?? "フィードバック無応答";
    case "skipped":
      return record.detail ?? "中断";
    case "running":
      return "応答待ち";
    case "pending":
    default:
      return "未開始";
  }
}

/**
 * daisyUI の table-xs は本文を .6875rem に固定する。ルートの clamp() 由来の
 * 相対サイズから外れて読みづらくなるため、セル側で明示的に上書きする。
 */
const CELL_CLASS = "text-[0.85em]";

function MotorRow({ record, isCurrent }: { record: MotorCheckRecord; isCurrent: boolean }) {
  const result: MotorCheckResult =
    isCurrent && record.result === "pending" ? "running" : record.result;
  const style = RESULT_STYLES[result];
  const description = describeRecord({ ...record, result });

  return (
    <tr>
      <td className={CELL_CLASS}>
        <div className="flex min-w-0 flex-col">
          <span className="truncate font-medium">{record.motor}</span>
          <span className="font-mono text-base-content/60">bus: {record.bus}</span>
        </div>
      </td>
      <td className={`${CELL_CLASS} text-right`}>
        <div className="flex flex-col items-end gap-px">
          <StatusBadge tone={style.tone}>{style.label}</StatusBadge>
          <span className="text-base-content/70">{description}</span>
        </div>
      </td>
    </tr>
  );
}

export function MotorCheckPanel({ robotName, isOpen, onOpenChange }: MotorCheckPanelProps) {
  const { state, start, abort } = useMotorCheck(robotName);

  const isRunning = state.status === "running";
  const isError = state.status === "error";
  const overall = state.snapshot?.overall ?? (isRunning ? "running" : null);
  const overallStyle = overall ? OVERALL_STYLES[overall] : null;

  const total = state.progress?.total ?? state.records.length;
  const index = state.progress?.index ?? state.records.length;
  const percent = total > 0 ? Math.min(100, Math.round((index / total) * 100)) : 0;

  const footerLabel = isRunning
    ? "実行中..."
    : state.status === "completed"
      ? "完了"
      : isError
        ? "失敗"
        : "未実行";

  return (
    <Modal
      open={isOpen}
      onClose={() => onOpenChange(false)}
      tone="danger"
      title={`MOTOR CHECK — ${robotName}`}
      boxClassName="min-w-[min(560px,80vw)]"
      bodyClassName="flex flex-col gap-3"
      footer={
        <div className="flex w-full items-center justify-between">
          <span className="text-base-content/70">{footerLabel}</span>
          <div className="flex gap-2">
            {isRunning ? (
              <Button tone="danger" onClick={abort}>
                <Icon as={Square} />
                中断
              </Button>
            ) : state.records.length > 0 || isError ? (
              <Button tone="info" onClick={start}>
                <Icon as={Play} />
                リトライ
              </Button>
            ) : null}
            <Button onClick={() => onOpenChange(false)}>閉じる</Button>
          </div>
        </div>
      }
    >
      {overallStyle ? (
        <div className="flex items-center justify-between">
          <span className="text-base-content/70">OVERALL</span>
          <StatusBadge tone={overallStyle.tone}>{overallStyle.label}</StatusBadge>
        </div>
      ) : null}

      {isRunning ? (
        <div className="flex flex-col gap-1">
          <div className="flex items-center justify-between">
            <span className="font-mono text-base-content/70 tabular-nums">
              {index} / {total}
            </span>
            <span className="truncate text-info">{state.current ?? "—"}</span>
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

      {isError ? (
        <div className="text-error">
          <p className="flex items-center gap-1.5 font-medium">
            <Icon as={TriangleAlert} />
            エラー
          </p>
          <p className="mt-1">{state.error}</p>
        </div>
      ) : null}

      {state.records.length === 0 && !isRunning && !isError ? (
        <p className="px-1 py-3 text-base-content/70">動作確認はまだ実行されていません。</p>
      ) : (
        <table className="table table-zebra table-xs">
          <tbody>
            {state.records.map((record) => (
              <MotorRow
                key={record.motor}
                record={record}
                isCurrent={isRunning && state.current === record.motor}
              />
            ))}
          </tbody>
        </table>
      )}
    </Modal>
  );
}
