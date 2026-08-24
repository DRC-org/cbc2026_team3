import path from "node:path";
import { fileURLToPath } from "node:url";

import { cloudflare } from "@cloudflare/vite-plugin";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

export default defineConfig(({ command, isPreview }) => {
  // dev サーバーは制御 PC 上のローカル UI 開発専用で Worker ランタイムを必要としない。
  // miniflare (workerd) の起動を省くことで起動時間と外部通信への依存をなくす。
  // build / preview は Cloudflare へのデプロイ結果と一致させるため有効のままにする
  const useCloudflare = command === "build" || isPreview === true;

  return {
    plugins: [react(), ...(useCloudflare ? [cloudflare()] : [])],
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
  };
});
