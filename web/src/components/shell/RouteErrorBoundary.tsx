import { TriangleAlert } from "lucide-react";
import { Component } from "react";
import type { ErrorInfo, ReactNode } from "react";

import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";

interface Props {
  children: ReactNode;
}

interface State {
  error: Error | null;
}

/**
 * タブ 1 枚の描画例外を、そのタブの中に閉じ込める。
 *
 * **境界が 1 枚も無いと、どこか 1 箇所の描画例外で React ツリー全体が
 * アンマウントし、ヘッダーの EMG STOP ボタンごと画面から消える。** 操縦者に
 * 残るのは白い画面と、動き続けている機体だけになる。実際に `state.safety` の
 * 1 欄が配信から落ちるだけでその状態が成立していた
 * (`describeSafetyIssues` はレンダー本体から呼ばれる)。
 *
 * したがって囲うのは `<Outlet />` だけで、ヘッダー・接続バナー・緊急停止
 * オーバーレイは境界の**外**に置く。ここを広げて外枠まで囲うと、
 * 「止める手段が残る」という境界の唯一の目的が消える。
 *
 * 復帰はリロードだけ。壊れた原因は配信内容にあることが多く、状態を持ったまま
 * 再描画しても同じ場所で投げ直すため、「もう一度描いてみる」ボタンは置かない。
 */
export class RouteErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // 画面には要約しか出せない。原因の特定に要る stack はコンソールへ残す
    console.error("画面の描画に失敗しました", error, info.componentStack);
  }

  render(): ReactNode {
    const { error } = this.state;
    if (!error) return this.props.children;

    return (
      <div
        role="alert"
        className="flex min-h-0 flex-1 flex-col items-center justify-center gap-3 p-6"
      >
        <p className="flex items-center gap-2 text-[1.4em] font-semibold text-error">
          <Icon as={TriangleAlert} />
          この画面の描画に失敗しました
        </p>
        {/* 最初に伝えるのは「機体を止められるか」。次の一手がそこで決まる */}
        <p className="text-center text-base-content/70">
          上部の EMG STOP は生きています。機体を止める必要があるならそのまま押してください。
        </p>
        <p className="text-center text-base-content/70">
          画面を再読み込みすると復帰します。他のタブへ切り替えても構いません。
        </p>
        <Button tone="info" onClick={() => window.location.reload()}>
          再読み込み
        </Button>
        <p className="max-w-[40em] text-center font-mono text-[0.85em] break-all text-base-content/50">
          {error.message}
        </p>
      </div>
    );
  }
}
