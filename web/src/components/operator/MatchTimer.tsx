import { useEffect, useReducer, useRef } from "react";

import { Panel } from "@/components/ui/Panel";
import type { MatchTimer as MatchTimerValue } from "@/lib/protocol";

/**
 * 試合時間の残りを表示する。**全デバイスで同じ値が出ること**がこの部品の存在理由。
 *
 * サーバーが配るのは残り時間ではなく「配信瞬間の経過ミリ秒」で、ここはそれを
 * アンカーにして自分の単調時計 (`performance.now()`) で進める。したがって
 * デバイス間のずれは WS の片道遅延ぶん (数 ms) に収まり、**操縦者の PC と
 * Monitor の壁時計が揃っている必要がない**。`Date.now()` を使うと NTP 補正で
 * 試合中に残り時間が飛ぶため使わない。
 *
 * サーバーが毎秒「残り何秒」を配る方式は採れない。`match_state` は変化時のみ
 * 配信することで `useRobotStatus()` を読む全画面の再描画を抑えており、毎秒
 * 変わる値を載せるとその前提が崩れる。加えて配信が詰まった 1 台ではタイマー
 * だけが凍り、WebSocket は開いたままなので操縦者は気付けない。
 */

/** 表示が変わる瞬間に起きるための余裕。境界ちょうどだと 1 周期取りこぼす */
const BOUNDARY_EPSILON_MS = 15;

interface Anchor {
  /** アンカー時点の経過ミリ秒 (サーバー配信値そのもの) */
  elapsedMs: number;
  /** アンカーを取った瞬間の単調時刻 */
  atPerfMs: number;
}

/** 残りミリ秒。0 未満へは落とさない (マイナス表示は競技時計として意味を持たない) */
function clampRemaining(remainingMs: number, durationMs: number): number {
  return Math.min(Math.max(remainingMs, 0), durationMs);
}

/**
 * 残りミリ秒を M:SS へ。**切り上げ**なのは、0:00 を「本当に時間が尽きた瞬間」
 * だけに出すため。切り捨てると残り 0.9 秒でも 0:00 と表示される。
 */
export function formatRemaining(remainingMs: number): string {
  const totalSeconds = Math.ceil(remainingMs / 1000);
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${minutes}:${String(seconds).padStart(2, "0")}`;
}

/** 次に表示が変わるまでのミリ秒。固定間隔で起こさない理由は下の useEffect を参照 */
function msUntilDisplayChange(remainingMs: number): number {
  return remainingMs - (Math.ceil(remainingMs / 1000) - 1) * 1000;
}

interface MatchTimerProps {
  timer: MatchTimerValue | null;
}

export function MatchTimer({ timer }: MatchTimerProps) {
  const anchor = useRef<Anchor>({ elapsedMs: 0, atPerfMs: 0 });
  const [, tick] = useReducer((n: number) => n + 1, 0);

  const elapsedMs = timer?.elapsed_ms ?? 0;
  const durationMs = timer?.duration_ms ?? 0;
  const running = timer?.running ?? false;

  // 配信が届くたびにアンカーを取り直す。取り直さないと、リロードした操縦者と
  // 途中から繋いだ Monitor だけが 0 から数え始め、画面ごとに違う残り時間が出る
  useEffect(() => {
    anchor.current = { elapsedMs, atPerfMs: performance.now() };
    tick();
  }, [elapsedMs, running]);

  // 秒表示が切り替わる瞬間に合わせて起こす。固定間隔 (setInterval) だと
  // デバイスごとに起床位相がずれ、同じ値を持っているのに秒の繰り上がりが
  // 最大 1 周期ぶん食い違って見える — 画面を並べたときに「同期していない」
  // と読めてしまい、この部品の目的そのものが崩れる。
  useEffect(() => {
    if (!running || durationMs <= 0) return;

    let timeoutId = 0;
    const schedule = () => {
      const elapsedNow = anchor.current.elapsedMs + (performance.now() - anchor.current.atPerfMs);
      const remaining = clampRemaining(durationMs - elapsedNow, durationMs);
      // 0:00 に達したら以降は表示が変わらない。空回りさせない
      if (remaining <= 0) return;

      timeoutId = window.setTimeout(
        () => {
          tick();
          schedule();
        },
        msUntilDisplayChange(remaining) + BOUNDARY_EPSILON_MS,
      );
    };
    schedule();

    return () => window.clearTimeout(timeoutId);
  }, [running, durationMs, elapsedMs]);

  // undefined も同じ扱いにする。null 一致だけで見ると、値が届いていない画面が
  // 残り 0:00 を確信して表示することになる
  if (!timer) {
    return (
      <Panel legend="試合時間" className="self-start">
        <div className="text-center text-[1.1em] text-base-content/60">タイマー未受信</div>
      </Panel>
    );
  }

  // 進行中だけ自分の時計で進める。停止中はサーバーが凍結した値をそのまま描く
  // (試合終了後に数字が進み続けると、何秒残して終えたのかが読めなくなる)
  const elapsedNow = running
    ? anchor.current.elapsedMs + (performance.now() - anchor.current.atPerfMs)
    : elapsedMs;
  const remaining = clampRemaining(durationMs - elapsedNow, durationMs);

  const caption = running ? "残り時間" : elapsedMs === 0 ? "開始前" : "試合終了時点の残り";

  return (
    <Panel legend="試合時間" className="self-start">
      <div className="flex flex-col items-center gap-[0.1em] py-1">
        <span className="font-mono text-[3.4em] leading-none font-bold tabular-nums">
          {formatRemaining(remaining)}
        </span>
        <span className="text-[0.8em] text-base-content/60">{caption}</span>
      </div>
    </Panel>
  );
}
