import { ManualAxisRow } from "@/components/operator/ManualAxisRow";
import { Panel } from "@/components/ui/Panel";
import { StatusBadge } from "@/components/ui/StatusBadge";
import type { RobotCommands } from "@/context/RobotContext";
import type { ManualState } from "@/lib/protocol";

interface AlwaysManualPanelProps {
  robotKey: string;
  manual: ManualState;
  /** 操作できない理由。null なら操作できる */
  blockedReason: string | null;
  /**
   * 送信できなかったら通知枠へ理由を出す送信口 (`useRobotCommands().sendOrReport`)。
   * 素の `send` を渡してはならない理由は `ManualPanel` と同じ (戻り値を捨てると、
   * 切断中に押した 1 回が痕跡なく消える)。
   */
  sendOrReport: RobotCommands["sendOrReport"];
}

/**
 * 半自動シーケンス制御のまま操作できる軸だけを並べる面。
 *
 * **対象を決めるのは配信された `manual_always` だけで、軸名は書かない。** 宣言の正は
 * 位置定数 yaml (`axes.<軸>.manual_always`) にあり、増減しても UI は無変更で済む。
 * 宣言できるのは到達判定を持たない単独軸 (duty / on_off) に限られるので、ここに並ぶ
 * 行は必ずプリセットのみの 1 軸 1 行になる。
 *
 * **`=== true` で厳密に見る。** 欄が落ちた配信では対象が 0 本になり、パネルごと
 * 描かれない —— シーケンス実行中に押せるボタンが、配信の欠落で増える側へ倒れては
 * ならない (理由は `lib/protocol.ts` の `manual_always`)。
 *
 * **対象が 1 本も無いロボットでは何も描かない。** 空の枠が常に置かれていると、
 * その機体に何か操作できるものがあるように読める。
 */
export function AlwaysManualPanel({
  robotKey,
  manual,
  blockedReason,
  sendOrReport,
}: AlwaysManualPanelProps) {
  const axes = manual.axes.filter((axis) => axis.manual_always === true);
  if (axes.length === 0) return null;

  // サーバーが通すのは `manual_move` だけ (対象軸は `manual:` を持たないので
  // 連続値の口はそもそも開いていない)。`onJog` / `onSet` は行の props の形を
  // 既存に合わせるためだけに置く —— ここから呼ばれる経路は無い
  const onMove = (axis: string, position: string) =>
    sendOrReport({ type: "manual_move", robot: robotKey, axis, position }, "プリセット移動");
  const onJog = (axis: string, delta: number) =>
    sendOrReport({ type: "manual_jog", robot: robotKey, axis, delta }, "ジョグ");
  const onSet = (axis: string, value: number) =>
    sendOrReport({ type: "manual_set", robot: robotKey, axis, value }, "目標値の送信");

  return (
    <Panel
      legend="常時操作"
      // 左カラムは flex-col。このパネルは中身で高さが決まり切っていて内部スクロールも
      // 持たないので、縦が足りないときに縮む側は隣のステップ一覧 (内部スクロールを持つ)
      // に引き受けさせる。ActionPanel と同じ扱いである。
      // **クロス軸 (align-self) の指定は書かない** —— flex-col では横方向に効くので、
      // 幅が内容ぶんへ落ちて列より縮む (grid の子へ書くものと取り違えやすい)
      className="shrink-0"
      // 列数を決めるコンテナクエリは**この本文 div** を基準にする。同じ要素へ
      // container 指定と幅の条件を書くと、条件は祖先の container に対して解決され、
      // 該当が無ければ静かに 1 列のままになる
      bodyClassName="@container p-0"
      actions={blockedReason ? <StatusBadge tone="error">{blockedReason}</StatusBadge> : null}
    >
      {/* 列数は幅で決める (呼び出し元は関与しない)。サブハンドは電磁弁 6 本が
          並ぶので、1 列のままだと ActionPanel の下で 6 行を占める */}
      <div className="grid @min-[40rem]:grid-cols-2 @min-[56rem]:grid-cols-3">
        {axes.map((axis) => (
          <ManualAxisRow
            key={axis.name}
            axis={axis}
            blockedReason={blockedReason}
            // 選択はキーボードで軸を渡り歩く連続軸だけの概念。ここに持ち込むと、
            // `←` `→` が何も起こさない行を選択できてしまう
            selected={false}
            onSelect={() => {}}
            onJog={onJog}
            onSet={onSet}
            onMove={onMove}
          />
        ))}
      </div>

      {/* **上書きされることを先に断る。** シーケンスは後からこれらの軸へ書きに来るので、
          手動で止めたコンベアは次にそれを回すステップが来れば再び回る。断っておかないと
          「押したのに戻った」が故障に見える */}
      <p className="shrink-0 border-t border-base-300 px-2 py-1 text-[0.8em] text-base-content/55">
        シーケンスが後からこの軸へ書き直します（手動の値は上書きされます）。
      </p>
    </Panel>
  );
}
