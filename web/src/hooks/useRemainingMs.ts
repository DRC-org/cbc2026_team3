import { useEffect, useReducer, useRef } from "react";

import type { MatchTimer as MatchTimerValue } from "@/lib/protocol";

/**
 * 試合の残りミリ秒を、表示が変わる瞬間だけ再描画しながら返す。
 *
 * **全デバイスで同じ値が出ること**がこのフックの存在理由。サーバーが配るのは
 * 残り時間ではなく「配信瞬間の経過ミリ秒」で、ここはそれをアンカーにして自分の
 * 単調時計 (`performance.now()`) で進める。したがってデバイス間のずれは WS の
 * 片道遅延ぶん (数 ms) に収まり、**操縦者の PC と Monitor の壁時計が揃っている
 * 必要がない**。`Date.now()` を使うと NTP 補正で試合中に残り時間が飛ぶため使わない。
 *
 * サーバーが毎秒「残り何秒」を配る方式は採れない。`match_state` は変化時のみ
 * 配信することで `useRobotStatus()` を読む全画面の再描画を抑えており、毎秒変わる
 * 値を載せるとその前提が崩れる。加えて配信が詰まった 1 台ではタイマーだけが凍り、
 * WebSocket は開いたままなので操縦者は気付けない。
 *
 * **算術をここ 1 箇所に閉じる。** 操縦者の `MatchTimer` と Monitor の `MatchStrip` が
 * 同じ残り時間を出すので、書き写すと「画面によって残り時間が違う」——どれが正しいか
 * 誰にも分からない状態——を作れてしまう (`lib/healthVerdict.ts` と同じ原則)。
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

/** 次に表示が変わるまでのミリ秒。固定間隔で起こさない理由は下の useEffect を参照 */
function msUntilDisplayChange(remainingMs: number): number {
  return remainingMs - (Math.ceil(remainingMs / 1000) - 1) * 1000;
}

/** 未受信は `null`。0 を返すと、値が届いていない画面が残り 0:00 を確信して表示する */
export function useRemainingMs(timer: MatchTimerValue | null | undefined): number | null {
  const [, tick] = useReducer((n: number) => n + 1, 0);

  const elapsedMs = timer?.elapsed_ms ?? 0;
  const durationMs = timer?.duration_ms ?? 0;
  const running = timer?.running ?? false;

  // **初回レンダーの時点でアンカーを確定させる。** アンカーを取り直す effect は
  // commit の後にしか走らないので、最初の 1 フレームはここの値がそのまま描かれる。
  // `atPerfMs: 0` を初期値にすると `performance.now() - 0` —— **ページを開いてからの
  // 経過ミリ秒** —— が「試合の経過」に化け、タブを開いて `duration_ms` (180 秒) 以上
  // 経ってからマウントされると `clampRemaining` が 0 へ丸めて **`0:00` を 1 フレーム
  // 描く**。試合中のタブ切替やリロードで踏み、視線を戻した一瞬にそれを見ると
  // 「時間切れ」に読める。
  const anchor = useRef<Anchor>({ elapsedMs, atPerfMs: performance.now() });

  // 配信が届くたびにアンカーを取り直す。取り直さないと、リロードした操縦者と
  // 途中から繋いだ Monitor だけが 0 から数え始め、画面ごとに違う残り時間が出る
  useEffect(() => {
    anchor.current = { elapsedMs, atPerfMs: performance.now() };
    tick();
  }, [elapsedMs, running]);

  // 秒表示が切り替わる瞬間に合わせて起こす。固定間隔 (setInterval) だとデバイスごとに
  // 起床位相がずれ、同じ値を持っているのに秒の繰り上がりが最大 1 周期ぶん食い違って
  // 見える — 画面を並べたときに「同期していない」と読めてしまい、目的そのものが崩れる。
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
  if (!timer) return null;

  // 進行中だけ自分の時計で進める。停止中はサーバーが凍結した値をそのまま描く
  // (試合終了後に数字が進み続けると、何秒残して終えたのかが読めなくなる)
  const elapsedNow = running
    ? anchor.current.elapsedMs + (performance.now() - anchor.current.atPerfMs)
    : elapsedMs;
  return clampRemaining(durationMs - elapsedNow, durationMs);
}
