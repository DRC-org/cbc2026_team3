import { useEffect, useRef } from "react";

import { useModalRegistry } from "@/context/ModalContext";
import { isHotkeyBlocked } from "@/hooks/useHotkeys";
import { useRepeatController } from "@/hooks/useRepeatController";

/**
 * 「キーを押している間くり返す」制御。手動操縦のジョグをキーボードから出すために使う。
 *
 * `useHotkeys` と分けてあるのは、あちらが `event.repeat` を弾く —— 押しっぱなしを
 * 意図的に無効化している —— ため。トリガーの多重送信を防ぐその判断は正しく、
 * ジョグだけが逆に「押している間くり返す」を必要とする。
 *
 * **止める側を多重に張る。** `useHoldRepeat` と同じ理由で、停止経路を 1 つでも
 * 取りこぼすと指を離したのに機体が動き続ける。キーボードで要るのは 4 つ:
 *
 * - `keyup` —— 通常の離し
 * - `window` の `blur` —— Alt+Tab 等でフォーカスごと奪われると `keyup` が来ない
 * - `visibilitychange` —— タブが背面へ回ると同上
 * - `enabled` の解除 / アンマウント —— 緊急停止・切断・軸選択の移動・モード離脱
 *
 * 最後の砦が指令側の可動範囲クランプであることも `useHoldRepeat` と同じ。
 */
export function useHoldKey(
  key: string,
  fire: (multiplier: number) => void,
  enabled: boolean,
  maxMultiplier = 1,
): { multiplier: number } {
  const { openCount } = useModalRegistry();
  const modalOpenRef = useRef(openCount > 0);
  modalOpenRef.current = openCount > 0;

  const { start, stop, multiplier } = useRepeatController(fire, enabled, maxMultiplier);

  useEffect(() => {
    if (!enabled) return;

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== key) return;
      // 入力欄での矢印キーはカーソル移動。`event.repeat` もここで落ちるので、
      // 連続発火を駆動するのは常に自前のタイマー 1 本だけになる
      if (modalOpenRef.current || isHotkeyBlocked(event)) return;
      event.preventDefault();
      start();
    };
    const onKeyUp = (event: KeyboardEvent) => {
      if (event.key === key) stop();
    };
    const onHidden = () => {
      if (document.visibilityState === "hidden") stop();
    };

    window.addEventListener("keydown", onKeyDown);
    window.addEventListener("keyup", onKeyUp);
    window.addEventListener("blur", stop);
    document.addEventListener("visibilitychange", onHidden);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("keyup", onKeyUp);
      window.removeEventListener("blur", stop);
      document.removeEventListener("visibilitychange", onHidden);
      // 押している最中に無効化・アンマウントされても止める
      stop();
    };
  }, [key, enabled, start, stop]);

  return { multiplier };
}
