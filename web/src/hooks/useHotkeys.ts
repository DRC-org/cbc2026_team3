import { useEffect, useRef } from "react";

import { useModalRegistry } from "@/context/ModalContext";

export type HotkeyMap = Record<string, (() => void) | undefined>;

/**
 * 入力中・修飾キー併用など、グローバルショートカットを受け付けない状況。
 * 競技中の誤爆は機体の破損に直結するため、少しでも曖昧な状況では発火させない。
 *
 * **押しっぱなしのジョグ (`useHoldKey`) もこの判定を共有する。** 目標値の入力欄で
 * 数字を打ちながら `←` を押すのはカーソル移動であって、機体を動かす操作ではない。
 * 判定を写すと、片方だけが入力欄を素通しにしても画面からは区別が付かない。
 */
export function isHotkeyBlocked(event: KeyboardEvent): boolean {
  // 押しっぱなしによる連続発火はトリガーの多重送信になるため弾く
  if (event.ctrlKey || event.metaKey || event.altKey || event.repeat) return true;

  const target = event.target as HTMLElement | null;
  if (target?.isContentEditable) return true;
  const tag = target?.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
}

/**
 * window 単位のキーバインドを登録する。キーは KeyboardEvent.key（スペースは " "）。
 *
 * ハンドラが見つかった場合は preventDefault する。直前にクリックしたボタンへ
 * フォーカスが残っていても Space が「そのボタンの再実行」にならず、常に
 * ここで定義した操作に一意に決まるようにするため。
 *
 * モーダル表示中は背後の画面を操作させない（緊急停止オーバーレイの裏で
 * シーケンスが進むのを防ぐ）。判定は ModalProvider が持つ表示中モーダル数による。
 */
export function useHotkeys(map: HotkeyMap, enabled = true): void {
  const { openCount } = useModalRegistry();

  const mapRef = useRef(map);
  mapRef.current = map;
  // リスナを張り直さずに最新値を読むためだけの参照。モーダル開閉で
  // 登録・解除を繰り返すとキー入力を取りこぼす余地が生まれる
  const modalOpenRef = useRef(openCount > 0);
  modalOpenRef.current = openCount > 0;

  useEffect(() => {
    if (!enabled) return;

    const onKeyDown = (event: KeyboardEvent) => {
      if (modalOpenRef.current || isHotkeyBlocked(event)) return;
      const handler = mapRef.current[event.key];
      if (!handler) return;
      event.preventDefault();
      handler();
    };

    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [enabled]);
}
