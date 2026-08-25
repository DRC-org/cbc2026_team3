import { createContext, useCallback, useContext, useMemo, useState } from "react";
import type { ReactNode } from "react";

interface ModalRegistry {
  /** 開いているモーダルの数。0 でなければ背面のホットキーを止める */
  openCount: number;
  /** 表示中モーダルの登録。戻り値を呼ぶと解除される（多重呼び出しは無視される） */
  register: () => () => void;
}

/**
 * Provider の外で描画された場合の既定値。
 * 単体テストのようにモーダル 1 つだけを切り出して描画する状況では、
 * 背面のホットキーが存在しないため封じる相手もいない。
 */
const NO_PROVIDER: ModalRegistry = { openCount: 0, register: () => () => {} };

const ModalContext = createContext<ModalRegistry>(NO_PROVIDER);

/**
 * 表示中モーダルの登録簿。
 *
 * 「モーダル表示中は背後の画面を操作させない」は安全機構であり
 * （緊急停止オーバーレイの裏でシーケンスが進むのを防ぐ）、
 * CSS クラス名の DOM 検索に依存させるとスタイル変更で静かに壊れる。
 * React の状態として持ち、テストで検証できる形にする。
 */
export function ModalProvider({ children }: { children: ReactNode }) {
  const [openCount, setOpenCount] = useState(0);

  const register = useCallback(() => {
    setOpenCount((count) => count + 1);
    let released = false;
    return () => {
      if (released) return;
      released = true;
      setOpenCount((count) => Math.max(0, count - 1));
    };
  }, []);

  const value = useMemo(() => ({ openCount, register }), [openCount, register]);

  return <ModalContext.Provider value={value}>{children}</ModalContext.Provider>;
}

export function useModalRegistry(): ModalRegistry {
  return useContext(ModalContext);
}
