import { TriangleAlert } from "lucide-react";

import { Icon } from "@/components/ui/Icon";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { MALFORMED } from "@/lib/protocol";
import type { SwitchMeasureSnapshot, SwitchMeasurement } from "@/lib/protocol";
import type { SwitchMeasureOutcome } from "@/lib/switchMeasureStatus";

function Quantity({ value, unit }: { value: number; unit: string }) {
  return (
    <span className="font-mono tabular-nums">
      {value} {unit}
    </span>
  );
}

function ResultTable({ result }: { result: SwitchMeasurement }) {
  const rows: [string, number][] = [
    ["作動点", result.engage],
    ["離脱点", result.release],
    ["ON 区間", result.width],
    ["刻み", result.step],
  ];
  return (
    <div className="overflow-x-auto">
      <table className="table w-auto table-xs">
        <tbody>
          {rows.map(([label, value]) => (
            <tr key={label}>
              <th scope="row" className="font-normal text-base-content/70">
                {label}
              </th>
              <td className="text-right">
                <Quantity value={value} unit={result.unit} />
                {label === "刻み" && result.coarse_step !== null ? (
                  <span className="ml-2 text-base-content/70">
                    (粗刻み <Quantity value={result.coarse_step} unit={result.unit} />)
                  </span>
                ) : null}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function SwitchMeasureBadge({ outcome }: { outcome: SwitchMeasureOutcome }) {
  if (outcome === "running") return <StatusBadge tone="info">測定中</StatusBadge>;
  if (outcome === "failed") return <StatusBadge tone="warning">失敗</StatusBadge>;
  if (outcome === "done") return <StatusBadge tone="success">完了</StatusBadge>;
  return null;
}

/** 失敗理由と実測。読めなかった結果は黙って空にしない */
export function SwitchMeasureResult({ state }: { state: SwitchMeasureSnapshot }) {
  return (
    <>
      {state.error ? <p className="text-error">{state.error}</p> : null}

      {state.result === MALFORMED ? (
        <p className="flex items-center gap-1.5 text-warning">
          <Icon as={TriangleAlert} />
          作動点測定の結果を読み取れませんでした (配信の形が読めていません)
        </p>
      ) : state.result !== null ? (
        <ResultTable result={state.result} />
      ) : null}
    </>
  );
}
