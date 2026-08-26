// oxlint-disable import/no-unassigned-import
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "@/App";

// 会場にネットワークがある保証はないため自己ホストする。Google Fonts の <link> は
// 解決に失敗しても無言で素の sans-serif に落ちるだけで、当日まで気付けない
import "@fontsource-variable/inter";
import "@fontsource-variable/noto-sans-jp";
import "@fontsource-variable/jetbrains-mono";
import "@/index.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
