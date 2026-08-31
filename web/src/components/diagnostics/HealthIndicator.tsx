import { StatusBadge } from "@/components/ui/StatusBadge";
import type { BusHealth, BusHealthState, HealthSnapshot } from "@/lib/protocol";
import type { Tone } from "@/lib/tone";

/** CAN ヘルスは正常/劣化/停止/未取得の 4 段階しか取らない（info は使わない） */
type HealthTone = Exclude<Tone, "info">;

const TONE_LABEL: Record<HealthTone, string> = {
  success: "OK",
  warning: "DEGRADED",
  error: "DOWN",
  neutral: "未取得",
};

function busTone(state: BusHealthState): HealthTone {
  if (state === "ok") return "success";
  if (state === "degraded") return "warning";
  return "error";
}

export function formatAge(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || Number.isNaN(ms)) return "—";
  if (ms < 0) return "—";
  if (ms < 1000) return `${Math.round(ms)}ms 前`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s 前`;
  if (ms < 3_600_000) return `${Math.floor(ms / 60_000)}m 前`;
  return `${Math.floor(ms / 3_600_000)}h 前`;
}

/**
 * daisyUI の table-xs は本文を .6875rem に固定する。ルートの clamp() 由来の
 * 相対サイズから外れて読みづらくなるため、セル側で明示的に上書きする
 * （font-size は tr に当たっているので、td/th の直接指定が勝つ）。
 */
const CELL_CLASS = "text-[0.85em]";

function StatusTag({ tone }: { tone: HealthTone }) {
  return <StatusBadge tone={tone}>{TONE_LABEL[tone]}</StatusBadge>;
}

/**
 * 受信フレームの解釈失敗数 (`rx_err`) は判定 (`tone`) を動かさず、内訳としてだけ添える。
 * 降格させないのはサーバー側の意図的な判断 (lib/can_manager.py `_record_rx_error`) で、
 * 表示側がそれを覆すと本物の送信障害の警告と区別が付かなくなる。一方で数を伏せると
 * 「握り潰した受信失敗を数として残す」ことの意味が消え、操縦者は STALE のモータを前に
 * 断線と解釈失敗を切り分けられない。
 */
function BusRow({ bus }: { bus: BusHealth }) {
  const tone = busTone(bus.state);
  // 0 のときは出さない。平常時に無音であることが、出たときに意味を持つ条件
  const notes = [
    bus.bus_off ? "bus_off" : null,
    // bus_off とは原因が別 (インタフェース断)。同じ語で出すと復旧の手当てを誤る
    bus.rx_down ? "rx_down" : null,
    bus.tx_error_count > 0 ? `tx_err ${bus.tx_error_count}` : null,
    bus.rx_error_count > 0 ? `rx_err ${bus.rx_error_count}` : null,
  ].filter(Boolean);
  return (
    <tr>
      <td className={`${CELL_CLASS} truncate`}>{bus.name}</td>
      <td className={`${CELL_CLASS} font-mono text-base-content/70`}>{bus.channel}</td>
      <td className={`${CELL_CLASS} text-right`}>
        <StatusBadge tone={tone} detail={notes.length > 0 ? notes.join(" ") : undefined}>
          {TONE_LABEL[tone]}
        </StatusBadge>
      </td>
    </tr>
  );
}

/**
 * CAN バスの健全性。`SubsystemStatus` の CAN 節だけが使う。
 *
 * 表示は 1 通りしか持たない。以前は pill / card / compact / bus-only の 4 variant を
 * 抱えていたが、本番から呼ばれるのは bus-only だけで 165 行中およそ 60 行が
 * 到達不能だった。分岐が残っていると、直すとき「どの見た目が本番か」を
 * 呼び出し元まで辿らないと決められない。
 */
export function HealthIndicator({ health }: { health: HealthSnapshot | undefined }) {
  if (!health) {
    return (
      <div className="flex items-center justify-between gap-2">
        <span className="text-base-content/70">CAN</span>
        <StatusTag tone="neutral" />
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-center justify-between gap-2">
        <span className="text-base-content/70">{health.buses.length} 系統</span>
        <StatusTag tone={busTone(health.overall)} />
      </div>
      {health.buses.length === 0 ? (
        <div className="text-base-content/70">バス情報なし</div>
      ) : (
        <table className="table table-zebra table-xs">
          <tbody>
            {health.buses.map((bus) => (
              <BusRow key={bus.name} bus={bus} />
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
