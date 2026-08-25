import path from "node:path";
import { fileURLToPath } from "node:url";

import { cloudflare } from "@cloudflare/vite-plugin";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

// dev サーバーの /ws 中継先。制御プログラムを別ホストで動かす場合に上書きする
const DEV_WS_TARGET = process.env.DEV_WS_TARGET ?? "http://localhost:8080";

/**
 * Vite は Host ヘッダがこの一覧に無いリクエストを拒否する（DNS リバインディング対策）。
 * 操縦者は Tailscale の MagicDNS 名（短縮名 `drc` / FQDN `*.ts.net`）で開くため、
 * 既定の localhost 判定だけでは "Blocked request" になる。
 * 別名を使う場合は VITE_ALLOWED_HOSTS にカンマ区切りで足す
 */
const ALLOWED_HOSTS = [
  "drc",
  ".ts.net",
  ...(process.env.VITE_ALLOWED_HOSTS?.split(",")
    .map((host) => host.trim())
    .filter(Boolean) ?? []),
];

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
      // 既定の localhost bind では Tailscale / LAN 上の別端末（タブレット等）から届かない
      host: true,
      allowedHosts: ALLOWED_HOSTS,
      // UI は既定で同一 origin の /ws へ接続する。dev サーバー経由でも実機と同じ挙動に
      // するため、ここで制御プログラム（既定 8080）へ中継する
      proxy: {
        "/ws": {
          target: DEV_WS_TARGET,
          ws: true,
        },
      },
    },
    preview: {
      host: true,
      allowedHosts: ALLOWED_HOSTS,
    },
  };
});
