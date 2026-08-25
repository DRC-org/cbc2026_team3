import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";
import { useMotorCheck } from "@/hooks/useMotorCheck";
import type { MotorCheckOverall, MotorCheckRecord, MotorCheckResult } from "@/hooks/useRobotSocket";
import { cx } from "@/lib/cx";
import type { Tone } from "@/lib/tone";
import { TONE_PROGRESS_CLASS, TONE_TEXT_CLASS } from "@/lib/tone";

interface MotorCheckPanelProps {
  robotName: string;
  isOpen: boolean;
  onOpenChange: (open: boolean) => void;
}

// 各モータ結果を記号 + セマンティック色で表現する。
const RESULT_STYLES: Record<MotorCheckResult, { symbol: string; tone: Tone; label: string }> = {
  pending: { symbol: "○", tone: "neutral", label: "待機中" },
  running: { symbol: "►", tone: "info", label: "確認中" },
  passed: { symbol: "✓", tone: "success", label: "合格" },
  failed: { symbol: "✗", tone: "error", label: "失敗" },
  timeout: { symbol: "⚠", tone: "warning", label: "タイムアウト" },
  skipped: { symbol: "·", tone: "neutral", label: "中断" },
};

const OVERALL_STYLES: Record<MotorCheckOverall, { symbol: string; tone: Tone; label: string }> = {
  running: { symbol: "►", tone: "info", label: "実行中" },
  ok: { symbol: "✓", tone: "success", label: "全モータ合格" },
  partial: { symbol: "⚠", tone: "warning", label: "一部失敗" },
  failed: { symbol: "✗", tone: "error", label: "失敗" },
};

function formatNumber(value: number): string {
  if (Number.isInteger(value)) return value.toFixed(0);
  if (Math.abs(value) >= 100) return value.toFixed(1);
  return value.toFixed(2);
}

function describeRecord(record: MotorCheckRecord): string {
  switch (record.result) {
    case "passed":
      return record.observed === null
        ? `期待 ${record.expected}`
        : `期待 ${record.expected} → 観測 ${formatNumber(record.observed)}`;
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

function MotorRow({ record, isCurrent }: { record: MotorCheckRecord; isCurrent: boolean }) {
  const result: MotorCheckResult =
    isCurrent && record.result === "pending" ? "running" : record.result;
  const style = RESULT_STYLES[result];
  const description = describeRecord({ ...record, result });

  return (
    <div className="flex items-center justify-between gap-3 p-1">
      <div className="flex min-w-0 items-center gap-2">
        <span className={cx("w-4 shrink-0 text-center", TONE_TEXT_CLASS[style.tone])}>
          {style.symbol}
        </span>
        <div className="flex min-w-0 flex-col">
          <span className="truncate">{record.motor}</span>
          <span className="text-fg-dim">bus: {record.bus}</span>
        </div>
      </div>
      <div className={cx("flex flex-col items-end gap-px", TONE_TEXT_CLASS[style.tone])}>
        <span>{style.label}</span>
        <span className="opacity-80">{description}</span>
      </div>
    </div>
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
          <span className="text-fg-dim">{footerLabel}</span>
          <div className="flex gap-2">
            {isRunning ? (
              <Button tone="danger" onClick={abort}>
                ■ 中断
              </Button>
            ) : state.records.length > 0 || isError ? (
              <Button tone="info" onClick={start}>
                ► リトライ
              </Button>
            ) : null}
            <Button onClick={() => onOpenChange(false)}>閉じる</Button>
          </div>
        </div>
      }
    >
      {overallStyle ? (
        <div className="flex items-center justify-between">
          <span className="text-fg-dim">OVERALL</span>
          <span className={TONE_TEXT_CLASS[overallStyle.tone]}>
            [{overallStyle.symbol} {overallStyle.label}]
          </span>
        </div>
      ) : null}

      {isRunning ? (
        <div className="flex flex-col gap-1">
          <div className="flex items-center justify-between">
            <span className="text-fg-dim tabular-nums">
              {index} / {total}
            </span>
            <span className="truncate text-info">{state.current ?? "—"}</span>
          </div>
          <progress
            className={cx(
              "progress h-[0.9rem] w-full border border-line bg-base-300",
              TONE_PROGRESS_CLASS.info,
            )}
            value={percent}
            max={100}
          />
        </div>
      ) : null}

      {isError ? (
        <div className="text-error">
          <p>⚠ エラー</p>
          <p className="mt-1">{state.error}</p>
        </div>
      ) : null}

      {state.records.length === 0 && !isRunning && !isError ? (
        <p className="px-1 py-3 text-fg-dim">動作確認はまだ実行されていません。</p>
      ) : (
        <div className="striped flex flex-col">
          {state.records.map((record) => (
            <MotorRow
              key={record.motor}
              record={record}
              isCurrent={isRunning && state.current === record.motor}
            />
          ))}
        </div>
      )}
    </Modal>
  );
}
