// oxlint-disable import/no-unassigned-import
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "@/App";

import "the-new-css-reset/css/reset.css";
import "@/index.css";
import "@tsaito18/tuicss-react/styles.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
