import { useCallback, useEffect, useRef, useState } from "react";

/**
 * 1 回目の直後に 2 回目を受け付けない不感時間 [ms]。
 * マウスのダブルクリック（既定でおよそ 500ms 以内の 2 連打）や手の震えを
 * そのまま実行へ通さないための最小限の幅。
 */
export const ARM_GUARD_MS = 400;

/** 武装したまま放置されたとき、元の表示へ戻るまでの時間 [ms] */
export const ARM_TIMEOUT_MS = 4000;

/**
 * `idle` = 未武装 / `guard` = 武装済みだが不感時間中 / `armed` = 次の 1 回で発火。
 * 表示上 `guard` と `armed` は区別しない。不感時間は 400ms しかなく、そこだけ
 * 見た目が変わると操縦者には「押せたり押せなかったり」に見える。
 */
type ArmState = "idle" | "guard" | "armed";

export interface ArmedPress {
  /** 武装中か。ボタンの文言と色を切り替えるために使う */
  armed: boolean;
  press: () => void;
  disarm: () => void;
}

/**
 * 同じボタンの二度押しで実行する操作の発火制御。試合開始・試合終了に使う。
 *
 * 確認ダイアログの代わりであり、目的は**カーソルを動かさずに確認を取ること**。
 * ダイアログは押したボタンから離れた位置に出るため、押す → カーソルを運ぶ →
 * 押す、の往復が要る。二度押しなら視線もカーソルも同じ場所に留まる。
 *
 * 誤爆を防ぐのは 2 つの時間だけで、どちらも欠かせない:
 * - **不感時間** — 無いと物理的なダブルクリック 1 回がそのまま「二度押し」として
 *   成立し、確認の役目を果たさない
 * - **自動解除** — 無いと、武装したまま忘れられたボタンが「次に触れた 1 回で
 *   試合が始まる」状態で画面に残り続ける
 *
 * 呼び出し側は、操作が成立しなくなった時点（切断・フェーズ遷移など）で
 * `disarm()` を呼ぶこと。武装は押した瞬間の状況に紐づいており、状況が変わった
 * 後の 1 回目を 2 回目として扱ってはならない。
 */
export function useArmedPress(fire: () => void): ArmedPress {
  const fireRef = useRef(fire);
  fireRef.current = fire;

  // 判定は ref が正。state は描画のためだけに持つ。
  // 判定を state 更新関数の中で行うと、React が更新関数を 2 度呼ぶ場面
  // (StrictMode) で発火まで 2 度走る
  const stateRef = useRef<ArmState>("idle");
  const [armed, setArmed] = useState(false);

  const guardRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const timeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const clearTimers = useCallback(() => {
    if (guardRef.current !== null) {
      clearTimeout(guardRef.current);
      guardRef.current = null;
    }
    if (timeoutRef.current !== null) {
      clearTimeout(timeoutRef.current);
      timeoutRef.current = null;
    }
  }, []);

  const transition = useCallback((next: ArmState) => {
    stateRef.current = next;
    setArmed(next !== "idle");
  }, []);

  const disarm = useCallback(() => {
    clearTimers();
    transition("idle");
  }, [clearTimers, transition]);

  const press = useCallback(() => {
    const prev = stateRef.current;

    // 不感時間中の 2 回目は捨てる。武装は維持し、自動解除までの残り時間も
    // 引き直さない（猶予は 1 回目からの 4 秒である）
    if (prev === "guard") return;

    if (prev === "armed") {
      clearTimers();
      transition("idle");
      fireRef.current();
      return;
    }

    clearTimers();
    transition("guard");
    guardRef.current = setTimeout(() => {
      guardRef.current = null;
      if (stateRef.current === "guard") transition("armed");
    }, ARM_GUARD_MS);
    timeoutRef.current = setTimeout(() => {
      timeoutRef.current = null;
      clearTimers();
      transition("idle");
    }, ARM_TIMEOUT_MS);
  }, [clearTimers, transition]);

  // アンマウント後にタイマーが残らないようにする（武装したまま画面を離れる経路がある）
  useEffect(() => clearTimers, [clearTimers]);

  return { armed, press, disarm };
}
