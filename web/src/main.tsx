// oxlint-disable import/no-unassigned-import
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "@/App";
import { legacyHashTarget } from "@/lib/tabs";

// index.css はライブラリの配色を後勝ちで差し替えるため、必ず最後に読み込む
import "the-new-css-reset/css/reset.css";
import "@tsaito18/tuicss-react/styles.css";
import "@/index.css";

// ルーター生成より前に旧ブックマーク (#main-hand) をパスへ書き換える。
// 描画後に遷移させると Monitor が一瞬映り、担当タブを開いたつもりの操縦者が戸惑う
const legacyTarget = legacyHashTarget(window.location);
if (legacyTarget) {
  window.history.replaceState(null, "", legacyTarget);
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
