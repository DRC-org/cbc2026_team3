import { useRepeatController } from "@/hooks/useRepeatController";

export { HOLD_ACCEL_EVERY, HOLD_DELAY_MS, HOLD_INTERVAL_MS } from "@/hooks/useRepeatController";

export interface HoldHandlers {
  onPointerDown: () => void;
  onPointerUp: () => void;
  onPointerLeave: () => void;
  onPointerCancel: () => void;
  onBlur: () => void;
}

export interface HoldRepeat {
  /** ボタンへそのまま展開するイベントハンドラ */
  handlers: HoldHandlers;
  /** 現在の実効倍率。ボタンの表示を実際に送る量と一致させるために返す */
  multiplier: number;
}

/**
 * 「押している間くり返す」ボタンの発火制御。手動操縦のジョグに使う。
 *
 * **止める側を多重に張る。** 発火を止められるのは `pointerup` だけではない ——
 * ボタンの外へドラッグして離す、ブラウザがポインタ操作を取り消す、タブが
 * 切り替わってフォーカスを失う、といった経路がある。1 つでも拾い損ねると
 * 指を離したのに機体が動き続ける。
 *
 * `setPointerCapture` を使ってはならない。捕捉すると `pointerleave` が
 * 飛ばなくなり、ボタンの外へ逃がして止める経路が消える。
 *
 * それでも取りこぼした場合の最後の砦は、指令側の可動範囲クランプである
 * (`axes.<軸>.manual` の min/max)。連続発火は必ず範囲の端で止まる。
 *
 * `maxMultiplier` を 1 より大きくすると、押し続けたぶんだけ 1 回の量が伸びる
 * (`useRepeatController`)。**伸びた量は呼び出し側が画面に出すこと** —— 押した量が
 * 読めないまま動く距離だけ変わると、操縦者は次の 1 押しの結果を予測できない。
 */
export function useHoldRepeat(
  fire: (multiplier: number) => void,
  enabled = true,
  maxMultiplier = 1,
): HoldRepeat {
  const { start, stop, multiplier } = useRepeatController(fire, enabled, maxMultiplier);

  return {
    handlers: {
      onPointerDown: start,
      onPointerUp: stop,
      onPointerLeave: stop,
      onPointerCancel: stop,
      onBlur: stop,
    },
    multiplier,
  };
}
