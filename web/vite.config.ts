import path from "node:path";
import { fileURLToPath } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  server: {
    // UI は同一 origin の /ws へ接続する。dev サーバー経由でも実機と同じ挙動にするため、
    // ここで制御プログラム (既定 8080) へ中継する
    proxy: {
      "/ws": {
        target: "http://localhost:8080",
        ws: true,
      },
    },
  },
});
