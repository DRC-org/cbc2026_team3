# 画面のスクリーンショット

`fix/ui-ux-round1` の PR 用に、**同じ手順・同じ状態**で撮った Before / After の対。
1366×768（会場へ持ち込む最小の機材）・`uv run python main.py --dry-run --dev-tools`。

| 対 | 画面 | 何が変わったか |
|---|---|---|
| `*-monitor-match.png` | Monitor・試合中 | 残り時間が出る / モータが 0 基 → 5 基見える / 空の EventFeed が 27% → 1 行 |
| `*-setup-checklist.png` | Monitor・準備中（10 項目チェック後） | 「次」へ自動スクロールし、動作確認ボタンが画面内に来る |
| `*-startgate.png` | Monitor・準備中（全チェック後） | 同じ無励磁を「要確認」→「異常」と呼ぶ（タブの LED と語彙が揃う） |
| `*-operator-setup.png` | サブハンド・準備中（半自動） | 機体状態 1 枚だけ → これから流す手順が右列に出る |
| `*-manual-subhand.png` | サブハンド・手動操縦 | 電磁弁 6 + ポンプ 2 が画面外 → 8 個ともスクロールなしで押せる / 可動範囲バーにプリセットの刻み |

**撮り直すとき**も同じ手順で撮ること（差が見えなくなる）:

1. `--dry-run --dev-tools` で起動し、ブラウザを 1366×768 にする
2. サブハンドを開いたまま撮る（準備中・半自動）
3. サブハンドを手動操縦へ切り替えて撮る
4. Monitor で先頭 10 項目をチェックして撮る
5. DEV 全チェックで撮る
6. 二度押しで試合を開始して撮る

**Before は `main` の UI と `main` のサーバーで撮る。** UI だけを `main` に戻すと、
`state.manual.axes[].positions` の形が食い違って `RouteErrorBoundary` が発火し、
撮れるのは「この画面の描画に失敗しました」になる（`git checkout main -- web/src lib/manual.py`
まで戻してサーバーを起動し直すこと）。この版ずれは撮影だけの話ではない ——
両方向の症状と手当ては `docs/web/pitfalls.md`「`state` の既存欄の『形』を変えるとき」。
