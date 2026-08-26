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
| `src/lib/healthVerdict.ts`    | 機体の健全性判定（CAN・モータ・安全機構）の**単一情報源**                      |
| `src/lib/sequenceStatus.ts`   | シーケンスの実行状態判定の単一情報源（`running` 配信が根拠）                   |
| `src/lib/time.ts`             | 時刻の単位（`EpochSeconds` / `EpochMs`）と表示                                 |
| `src/test/ws-contract.json`   | サーバーの実配信サンプル。**生成物なので手で編集しない**                       |
| `src/test/`                   | vitest 共通ヘルパ。テスト本体は対象ソースの隣に `*.test.ts(x)`                 |

## 開発時に踏みやすい点

- **配色は `index.css` のテーマだけを触る。** 個別コンポーネントに色の生値を書かない
- **`applyLegacyHashRedirect()` は `createBrowserRouter()` より前。** 順序が崩れると
  操縦者がブックマークした `#main-hand` 等の URL が全て Monitor に落ちる
- **タブ遷移では `location.search` を落とさない。** `?ws=` の接続先上書きが失われる
- **サーバーとの契約は `src/test/wsContract.test.ts` が守る。** サンプルを手で書き写さず、
  `ws-contract.json`（Python 側が `UPDATE_WS_CONTRACT=1 uv run pytest tests/test_ws_contract.py`
  で生成）を import して受信経路へ流し込む。写した瞬間に「想像した契約」へ逆戻りし、
  実際に `health_change` が実機で 100% 捨てられていたことを両側のテストが揃って見逃した。
  生成物なので整形もしない（`.prettierignore` で oxfmt の対象外にしてある）
- **時刻は受信境界で ms へ正規化する。** サーバーはエポック秒、`Date` はミリ秒。
  UI 状態のフィールド名は `...Ms` で終わらせ、秒のままの値は `EpochSeconds` を名乗る
- **モーダルに `<dialog>` を使わない。** Esc で必ず閉じてしまい、緊急停止オーバーレイの
  「解除は Reset ボタンのみ」という安全設計と両立しない
