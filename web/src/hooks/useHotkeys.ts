import { useEffect, useRef } from "react";

export type HotkeyMap = Record<string, (() => void) | undefined>;

/**
 * グローバルショートカットを受け付けない状況。
 * 競技中の誤爆は機体の破損に直結するため、少しでも曖昧な状況では発火させない。
 */
function isBlocked(event: KeyboardEvent): boolean {
  // 押しっぱなしによる連続発火はトリガーの多重送信になるため弾く
  if (event.ctrlKey || event.metaKey || event.altKey || event.repeat) return true;

  const target = event.target as HTMLElement | null;
  if (target?.isContentEditable) return true;
  const tag = target?.tagName;
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return true;

  // モーダル表示中は背後の画面を操作させない (緊急停止オーバーレイの裏で
  // シーケンスが進むのを防ぐ)。TuiCss の Modal は開いている間だけ .active が付く
  return document.querySelector(".tui-modal.active") !== null;
}

/**
 * window 単位のキーバインドを登録する。キーは KeyboardEvent.key（スペースは " "）。
 *
 * ハンドラが見つかった場合は preventDefault する。直前にクリックしたボタンへ
 * フォーカスが残っていても Space が「そのボタンの再実行」にならず、常に
 * ここで定義した操作に一意に決まるようにするため。
 */
export function useHotkeys(map: HotkeyMap, enabled = true): void {
  const mapRef = useRef(map);
  mapRef.current = map;

  useEffect(() => {
    if (!enabled) return;

    const onKeyDown = (event: KeyboardEvent) => {
      if (isBlocked(event)) return;
      const handler = mapRef.current[event.key];
      if (!handler) return;
      event.preventDefault();
      handler();
    };

    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [enabled]);
}
