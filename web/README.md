# web — 操縦 UI

キャチロボバトルコンテスト 2026 出場ロボットの操縦 UI。
Vite + React + TypeScript + Tailwind v4 / daisyUI 5。

構造は [`../docs/architecture.md`](../docs/architecture.md)、崩してはならない設計と
その理由は [`../docs/invariants.md`](../docs/invariants.md) を参照。

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

| パス                           | 役割                                                                                                                                            |
| ------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| `src/App.tsx`                  | 旧ハッシュ URL の読み替え → `createBrowserRouter` の生成（**この順序に依存**）                                                                  |
| `src/routes.tsx`               | ルート定義                                                                                                                                      |
| `src/layouts/RootLayout.tsx`   | WebSocket 接続・Provider・ヘッダー（タブ・接続表示・時計）・通知・緊急停止オーバーレイ                                                          |
| `src/index.css`                | Tailwind の取り込みと daisyUI カスタムテーマ `cbc`（配色の単一情報源）                                                                          |
| `src/components/ui/`           | 自前プリミティブ（`Page` / `Panel` / `Section` / `Button` / `StatusBadge` / `Kbd` / `Icon` / `Modal`）。レイアウト骨格は CSS ではなくここが持つ |
| `src/lib/protocol.ts`          | WS メッセージの型と**受信条件**（最下層。UI の hook を import しない）                                                                          |
| `src/lib/robotReducer.ts`      | 受信 → UI 状態の遷移（純関数なので接続を張らずに検証できる）                                                                                    |
| `src/hooks/useWebSocket.ts`    | 接続・再接続・接続先切替だけ（メッセージの意味は解釈しない）                                                                                    |
| `src/hooks/useRobotSocket.ts`  | 上の 3 つを束ねて 1 つの UI 状態にする                                                                                                          |
| `src/context/RobotContext.tsx` | 配布。購読を**頻度で 3 つに分割**（states / status / commands）                                                                                 |
| `src/lib/healthVerdict.ts`     | 機体の健全性判定（CAN・モータ・安全機構）の**単一情報源**                                                                                       |
| `src/lib/sequenceStatus.ts`    | シーケンスの実行状態判定の単一情報源（`running` 配信が根拠）                                                                                    |
| `src/lib/time.ts`              | 時刻の単位（`EpochSeconds` / `EpochMs`）と表示                                                                                                  |
| `src/test/ws-contract.json`    | サーバーの実配信サンプル。**生成物なので手で編集しない**                                                                                        |
| `src/test/`                    | vitest 共通ヘルパ。テスト本体は対象ソースの隣に `*.test.ts(x)`                                                                                  |

## 開発時に踏みやすい点

UI 側の不変条件（受信境界の `MALFORMED`、context の分割、モーダル、EMG STOP の配置、
daisyUI の書き方、grid/flex の伸び方など）は
[`../docs/invariants.md`](../docs/invariants.md) の §8 にまとまっている。**画面を触る前に
一度読むこと。** ここに残すのは web/ の作業でしか出会わない点だけ。

- **配色は `index.css` のテーマだけを触る。** 個別コンポーネントに色の生値を書かない
- **タブ遷移では `location.search` を落とさない。** `?ws=` の接続先上書きが失われる
- **時刻は受信境界で ms へ正規化する。** サーバーはエポック秒、`Date` はミリ秒。UI 状態の
  フィールド名は `...Ms` で終わらせ、秒のままの値は `EpochSeconds` を名乗る
- **`ws-contract.json` は生成物なので手で書かず、整形もしない**
  （`.prettierignore` で oxfmt の対象外にしてある）。サーバー側を変えたら
  `UPDATE_WS_CONTRACT=1 uv run pytest tests/test_ws_contract.py` で作り直す
