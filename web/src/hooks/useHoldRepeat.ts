import { useCallback, useEffect, useRef } from "react";

/** 押し始めてから連続発火に入るまでの待ち [ms]。単発で離す操作を吸わない長さ */
export const HOLD_DELAY_MS = 400;
/** 連続発火の間隔 [ms] */
export const HOLD_INTERVAL_MS = 150;

export interface HoldHandlers {
  onPointerDown: () => void;
  onPointerUp: () => void;
  onPointerLeave: () => void;
  onPointerCancel: () => void;
  onBlur: () => void;
}

/**
 * 「押している間くり返す」ボタンの発火制御。手動操縦のジョグに使う。
 *
 * **止める側を多重に張る。** 発火を止められるのは `pointerup` だけではない —
 * ボタンの外へドラッグして離す、ブラウザがポインタ操作を取り消す、タブが
 * 切り替わってフォーカスを失う、といった経路がある。1 つでも拾い損ねると
 * 指を離したのに機体が動き続ける。
 *
 * `setPointerCapture` を使ってはならない。捕捉すると `pointerleave` が
 * 飛ばなくなり、ボタンの外へ逃がして止める経路が消える。
 *
 * それでも取りこぼした場合の最後の砦は、指令側の可動範囲クランプである
 * (`axes.<軸>.manual` の min/max)。連続発火は必ず範囲の端で止まる。
 */
export function useHoldRepeat(fire: () => void, enabled = true): HoldHandlers {
  const fireRef = useRef(fire);
  fireRef.current = fire;

  const delayRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const stop = useCallback(() => {
    if (delayRef.current !== null) {
      clearTimeout(delayRef.current);
      delayRef.current = null;
    }
    if (intervalRef.current !== null) {
      clearInterval(intervalRef.current);
      intervalRef.current = null;
    }
  }, []);

  const start = useCallback(() => {
    if (!enabled) return;
    stop();
    // 最初の 1 回は押した瞬間に出す。単発の操作が待たされないようにする
    fireRef.current();
    delayRef.current = setTimeout(() => {
      delayRef.current = null;
      intervalRef.current = setInterval(() => fireRef.current(), HOLD_INTERVAL_MS);
    }, HOLD_DELAY_MS);
  }, [enabled, stop]);

  // アンマウントでも必ず止める。タブを切り替えただけで送り続けないため
  useEffect(() => stop, [stop]);

  return {
    onPointerDown: start,
    onPointerUp: stop,
    onPointerLeave: stop,
    onPointerCancel: stop,
    onBlur: stop,
  };
}
