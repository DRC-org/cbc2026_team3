import { TriangleAlert } from "lucide-react";

import { Icon } from "@/components/ui/Icon";

interface ClearanceWarningProps {
  /** 動かす範囲。1 機だけの操作は "robot"、全機を回す動作確認は "all" */
  scope?: "robot" | "all";
}

/** 機体を動かす確認モーダルの退避喚起 */
export function ClearanceWarning({ scope = "robot" }: ClearanceWarningProps) {
  const target = scope === "all" ? "両機" : "この機体";
  return (
    <p className="mt-2 flex items-center gap-1.5 text-error">
      <Icon as={TriangleAlert} />
      {`${target}の可動範囲に人・物がないことを確認してから開始してください。`}
    </p>
  );
}

/** 手動操縦中に押されたとき、開始前に半自動へ戻すことの予告 */
export function SemiAutoRestoreNotice() {
  return <p className="mt-2">手動操縦を抜け、全機を半自動へ戻してから開始します。</p>;
}
