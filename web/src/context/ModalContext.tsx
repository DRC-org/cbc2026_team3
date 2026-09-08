import { createContext, useCallback, useContext, useMemo, useState } from "react";
import type { ReactNode } from "react";

interface ModalRegistry {
  openCount: number;
  register: () => () => void;
}

const NO_PROVIDER: ModalRegistry = { openCount: 0, register: () => () => {} };

const ModalContext = createContext<ModalRegistry>(NO_PROVIDER);

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
