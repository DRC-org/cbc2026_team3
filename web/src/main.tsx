// oxlint-disable import/no-unassigned-import
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "@/App";

// index.css はライブラリの配色を後勝ちで差し替えるため、必ず最後に読み込む
import "the-new-css-reset/css/reset.css";
import "@tsaito18/tuicss-react/styles.css";
import "@/index.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
