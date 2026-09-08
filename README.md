# cbc2026-team3-central

キャチロボバトルコンテスト 2026 出場ロボットの中央制御プログラム。

固定型ロボット（メインハンド + サブハンド）を半自動シーケンス制御で動かす。同一 PC 上で
両ロボットを制御し、操縦者は Web UI（`localhost:8080`）から操作する。自作モータドライバの
ファームウェアも同じリポジトリに同居している。

## 起動

```bash
uv run python main.py             # サーバー起動（localhost:8080）
uv run python main.py --dry-run   # CAN バスなしで起動（配線確認に使える）
uv run pytest                     # テスト
```

CAN バスのセットアップ、Web UI のビルド、systemd への配置は
[`docs/operations.md`](docs/operations.md) を参照。

## 文書の地図

| 読みたいこと | 文書 |
|---|---|
| **試合当日に手が止まった** | [`docs/venue_recovery.md`](docs/venue_recovery.md) — 会場カード。これ 1 枚 |
| 何がどう組まれているか | [`docs/architecture.md`](docs/architecture.md) |
| **なぜそう組んだか / 触ってはいけない理由** | [`docs/invariants.md`](docs/invariants.md) |
| 点検・ヘルス・動作確認は今どうなっているか | [`docs/checks_and_health.md`](docs/checks_and_health.md) |
| 自作モータドライバの CAN プロトコル | [`docs/motor_driver_can_protocol.md`](docs/motor_driver_can_protocol.md) |
| 機構が付いた日に埋める値 | [`docs/mechanism_handoff.md`](docs/mechanism_handoff.md) |
| コマンド・CAN セットアップ・サービス運用 | [`docs/operations.md`](docs/operations.md) |
| ファームウェアのビルドと書き込み | [`firmware/README.md`](firmware/README.md) |
| **操縦 UI（`web/`）が今どうなっているか** | [`docs/web/`](docs/web/) — 画面 / 配色・操作 / データの流れ / 罠 の 4 枚 |
| 操縦 UI の開発（コマンド・ビルド） | [`web/README.md`](web/README.md) |
| いつ何が起きたか（実機で観測した事象の記録） | [`docs/history/`](docs/history/) — 正ではない |

`docs/architecture.md` と `docs/invariants.md` は対になっている。**構造は architecture、
その構造を崩してはならない理由は invariants** にあり、同じ話を両方には書かない。

## ディレクトリ

| パス | 中身 |
|---|---|
| `main.py` | 起動。config を読み、CAN とシーケンスを配線し、サーバーを立てる |
| `lib/` | 共通ライブラリ（両ロボットで共有）。ドライバ・CAN・制御ループ・サーバー |
| `sequences/` | シーケンス定義。数値は持たず、位置定数 yaml を参照する |
| `config/` | YAML 設定。ロボット構成・位置定数・指差喚呼・机上ベンチ用の一式 |
| `web/` | 操縦 UI（Vite + React + TypeScript） |
| `firmware/` | 自作モータドライバのファームウェア（DC / サーボ / 電磁弁） |
| `scripts/` | CAN セットアップ・systemd unit・実機チューニング CLI |
| `tests/` | Python 側のテスト。UI は `web/src` 内に併置、ファームは `firmware/test/` |

## 言語

コミュニケーション・コメント・文書とも日本語。
