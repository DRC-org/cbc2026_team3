import {
  Button,
  Modal,
  ModalBody,
  ModalFooter,
  ModalHeader,
  ProgressBar,
} from "@tsaito18/tuicss-react";

import { useMotorCheck } from "@/hooks/useMotorCheck";
import type { MotorCheckOverall, MotorCheckRecord, MotorCheckResult } from "@/hooks/useRobotSocket";
import { cx } from "@/lib/cx";
import type { TuiColor } from "@/lib/tuiColor";

interface MotorCheckPanelProps {
  robotName: string;
  isOpen: boolean;
  onOpenChange: (open: boolean) => void;
}

// 各モータ結果を TUI 記号 + セマンティック色クラスで表現する。
const RESULT_STYLES: Record<
  MotorCheckResult,
  { symbol: string; textClass: string; label: string }
> = {
  pending: { symbol: "○", textClass: "secondary-text", label: "待機中" },
  running: { symbol: "►", textClass: "info-text", label: "確認中" },
  passed: { symbol: "✓", textClass: "success-text", label: "合格" },
  failed: { symbol: "✗", textClass: "danger-text", label: "失敗" },
  timeout: { symbol: "⚠", textClass: "warning-text", label: "タイムアウト" },
  skipped: { symbol: "·", textClass: "secondary-text", label: "中断" },
};

const OVERALL_STYLES: Record<
  MotorCheckOverall,
  { symbol: string; textClass: string; color: TuiColor; label: string }
> = {
  running: {
    symbol: "►",
    textClass: "info-text",
    color: "info",
    label: "実行中",
  },
  ok: {
    symbol: "✓",
    textClass: "success-text",
    color: "success",
    label: "全モータ合格",
  },
  partial: {
    symbol: "⚠",
    textClass: "warning-text",
    color: "warning",
    label: "一部失敗",
  },
  failed: {
    symbol: "✗",
    textClass: "danger-text",
    color: "danger",
    label: "失敗",
  },
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
    <div
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        gap: 12,
        padding: "4px",
      }}
    >
      <div style={{ display: "flex", minWidth: 0, alignItems: "center", gap: 8 }}>
        <span
          className={cx(style.textClass)}
          style={{
            width: "1rem",
            flexShrink: 0,
            textAlign: "center",
          }}
        >
          {style.symbol}
        </span>
        <div style={{ display: "flex", minWidth: 0, flexDirection: "column" }}>
          <span
            style={{
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}
          >
            {record.motor}
          </span>
          <span style={{ opacity: 0.6 }}>bus: {record.bus}</span>
        </div>
      </div>
      <div
        className={cx(style.textClass)}
        style={{
          display: "flex",
          flexDirection: "column",
          alignItems: "flex-end",
          gap: 2,
        }}
      >
        <span>{style.label}</span>
        <span style={{ opacity: 0.8 }}>{description}</span>
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
    <Modal open={isOpen} onClose={() => onOpenChange(false)} windowClassName="red-168 left-align">
      <ModalHeader>MOTOR CHECK — {robotName}</ModalHeader>
      <ModalBody
        className="tui-scroll-cyan"
        style={{
          display: "flex",
          flexDirection: "column",
          gap: 12,
          minWidth: "min(560px, 80vw)",
        }}
      >
        {overallStyle ? (
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
            }}
          >
            <span style={{ opacity: 0.8 }}>OVERALL</span>
            <span className={cx(overallStyle.textClass)}>
              [{overallStyle.symbol} {overallStyle.label}]
            </span>
          </div>
        ) : null}

        {isRunning ? (
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            <div
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
              }}
            >
              <span className="tabular-nums" style={{ opacity: 0.8 }}>
                {index} / {total}
              </span>
              <span
                className="info-text"
                style={{
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                  whiteSpace: "nowrap",
                }}
              >
                {state.current ?? "—"}
              </span>
            </div>
            <ProgressBar
              className="tui-bg-cyan-black"
              progressClassName="cyan-255"
              style={{ width: "100%" }}
              value={percent}
            />
          </div>
        ) : null}

        {isError ? (
          <div className="danger-text">
            <p>⚠ エラー</p>
            <p style={{ marginTop: 4, opacity: 0.9 }}>{state.error}</p>
          </div>
        ) : null}

        {state.records.length === 0 && !isRunning && !isError ? (
          <p style={{ padding: "12px 4px", opacity: 0.7 }}>動作確認はまだ実行されていません。</p>
        ) : (
          <div style={{ display: "flex", flexDirection: "column" }}>
            {state.records.map((record) => (
              <MotorRow
                key={record.motor}
                record={record}
                isCurrent={isRunning && state.current === record.motor}
              />
            ))}
          </div>
        )}
      </ModalBody>
      <ModalFooter style={{ width: "100%", alignItems: "center", justifyContent: "space-between" }}>
        <span style={{ opacity: 0.7 }}>{footerLabel}</span>
        <div style={{ display: "flex", gap: 8 }}>
          {isRunning ? (
            <Button className="red-255" onClick={abort}>
              ■ 中断
            </Button>
          ) : state.records.length > 0 || isError ? (
            <Button className="cyan-255" onClick={start}>
              ► リトライ
            </Button>
          ) : null}
          <Button onClick={() => onOpenChange(false)}>閉じる</Button>
        </div>
      </ModalFooter>
    </Modal>
  );
}
