# web — 操縦 UI

キャチロボバトルコンテスト 2026 出場ロボットの操縦 UI。
Vite + React + TypeScript + Tailwind v4 / daisyUI 5。

設計判断は `../docs/impl_plan.md`、リポジトリ全体の約束は `../CLAUDE.md` を参照。

## コマンド

```bash
pnpm install       # 依存インストール
pnpm dev           # 開発サーバー（全インターフェースに bind）
pnpm build         # プロダクションビルド（出力は dist/）
pnpm test          # vitest（watch）
pnpm test:run      # vitest（1 回だけ実行）
pnpm check         # lint + format + 型検査 + テスト
```

制御プログラム（`uv run python main.py`）が `dist/` をそのまま配信するため、
ビルド出力先は `dist/` から変えないこと（`../lib/server.py` が参照している）。

## 構成

| パス                          | 役割                                                                           |
| ----------------------------- | ------------------------------------------------------------------------------ |
| `src/App.tsx`                 | 旧ハッシュ URL の読み替え → `createBrowserRouter` の生成（**この順序に依存**） |
| `src/routes.tsx`              | ルート定義                                                                     |
| `src/layouts/RootLayout.tsx`  | WebSocket 接続・Provider・ヘッダー・タブ・ステータスバー                       |
| `src/index.css`               | Tailwind の取り込みと daisyUI カスタムテーマ `cbc`（配色の単一情報源）         |
| `src/components/ui/`          | 自前プリミティブ（`Button` / `Panel` / `Modal`）                               |
| `src/hooks/useRobotSocket.ts` | WebSocket 送受信とメッセージ型                                                 |
| `src/test/`                   | vitest 共通ヘルパ。テスト本体は対象ソースの隣に `*.test.ts(x)`                 |

## 開発時に踏みやすい点

- **配色は `index.css` のテーマだけを触る。** 個別コンポーネントに色の生値を書かない
- **`applyLegacyHashRedirect()` は `createBrowserRouter()` より前。** 順序が崩れると
  操縦者がブックマークした `#main-hand` 等の URL が全て Monitor に落ちる
- **タブ遷移では `location.search` を落とさない。** `?ws=` の接続先上書きが失われる
- **モーダルに `<dialog>` を使わない。** Esc で必ず閉じてしまい、緊急停止オーバーレイの
  「解除は Reset ボタンのみ」という安全設計と両立しない
