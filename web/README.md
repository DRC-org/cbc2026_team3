# web — 操縦 UI

キャチロボバトルコンテスト 2026 出場ロボットの操縦 UI。
Vite + React + TypeScript + Tailwind v4 / daisyUI 5。

**仕様は `../docs/web/` にある。UI を触る前にそこを読む。**

| 文書                                                   | 答える問い                                                 |
| ------------------------------------------------------ | ---------------------------------------------------------- |
| [`../docs/web/screens.md`](../docs/web/screens.md)     | どの画面に何が出るか（レイアウト・部品カタログ）           |
| [`../docs/web/design.md`](../docs/web/design.md)       | どう見せ、どう操作するか（配色・キーボード・確認の取り方） |
| [`../docs/web/data_flow.md`](../docs/web/data_flow.md) | 値がどう届き、どこで判定するか（WS 契約・受信境界）        |
| [`../docs/web/pitfalls.md`](../docs/web/pitfalls.md)   | **何で壊れるか。実際に踏んだ罠と、それを守っているテスト** |

設計判断の理由は `../CLAUDE.md`、経緯は `../docs/impl_plan.md` の Phase 4。

## コマンド

```bash
pnpm install       # 依存インストール
pnpm dev           # 開発サーバー（全インターフェースに bind）
pnpm build         # プロダクションビルド（出力は dist/）
pnpm test          # vitest（watch）
pnpm test:run      # vitest（1 回だけ実行）
pnpm check         # lint + format + 型検査 + テスト + ビルド
```

**ビルド出力先を `dist/` から変えないこと。** 制御プログラム（`uv run python main.py`）が
`../lib/server.py` からそのまま配信している。

開発サーバーの bind と `allowedHosts`、`/ws` のプロキシ先は
[`../docs/web/data_flow.md`](../docs/web/data_flow.md) の「開発サーバー経由で繋ぐとき」。
`@cloudflare/vite-plugin` は build / preview のみで有効にする（`vite.config.ts`）——
dev サーバーは制御 PC 上のローカル UI 開発専用で Worker ランタイムを必要とせず、
miniflare 起動に伴う `Request.cf` 取得（外部通信）と起動遅延を避けるため。

## ディレクトリ

| パス                             | 中身                                                                                                                  |
| -------------------------------- | --------------------------------------------------------------------------------------------------------------------- |
| `src/App.tsx` · `src/routes.tsx` | 起動とルート定義                                                                                                      |
| `src/layouts/`                   | 全画面共通の外枠（WS 接続・Provider・ヘッダー）                                                                       |
| `src/pages/`                     | Monitor（`Dashboard`）と操縦者（`RobotControl`）                                                                      |
| `src/components/`                | **誰が描くか**で 6 つ（`shell` / `monitor` / `operator` / `motorcheck` / `diagnostics` / `ui`）。直下には何も置かない |
| `src/hooks/`                     | 接続・ホットキー・長押し・二度押し                                                                                    |
| `src/context/`                   | 購読を頻度で 3 分割して配る                                                                                           |
| `src/lib/`                       | ワイヤ型・状態遷移・判定。**最下層なので `hooks/` を import しない**                                                  |
| `src/index.css`                  | Tailwind の取り込みと daisyUI カスタムテーマ `cbc`（配色の単一情報源）                                                |
| `src/test/`                      | vitest 共通ヘルパと `ws-contract.json`（**生成物。手で編集しない**）                                                  |

テスト本体は対象ソースの隣に `*.test.ts(x)` として置く。barrel（`index.ts`）は作らず、
import は常に実ファイルまで指す。
