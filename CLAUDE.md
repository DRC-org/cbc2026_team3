# CLAUDE.md

Claude Code（claude.ai/code）がこのリポジトリで作業するときの指針。

## プロジェクト概要

キャチロボバトルコンテスト 2026 出場ロボットの中央制御プログラム。固定型ロボット
（メインハンド + サブハンド）を半自動シーケンス制御で動作させる。同一 PC 上で両ロボットを
制御し、Web UI（localhost:8080）から操縦者が操作する。自作モータドライバのファームウェア
（PlatformIO / CubeMX）も同じリポジトリに同居している。

asyncio 単一プロセスで CAN 通信・シーケンス制御・Web サーバーを統合実行する。

## 文書の地図 — どれが「正」か

| 文書 | 何の正か |
|---|---|
| [`docs/invariants.md`](docs/invariants.md) | **崩してはならない設計と、その理由。** 実装判断で迷ったらここ |
| [`docs/architecture.md`](docs/architecture.md) | 何がどう組まれているか（構造・データフロー・契約） |
| [`docs/motor_driver_can_protocol.md`](docs/motor_driver_can_protocol.md) | 自作モータドライバ CAN プロトコル。PC 側 `lib/drivers/generic.py` と `firmware/` の双方がこれに従う |
| [`docs/checks_and_health.md`](docs/checks_and_health.md) | 点検とヘルスの「**今どうなっているか**」。ヘルス監視 / 動作確認 / 常駐保護 / 指差喚呼の 4 系統 |
| [`docs/web/`](docs/web/) | **操縦 UI の「今どうなっているか」。4 枚。** `screens.md`（画面・部品カタログ）/ `design.md`（配色・ラベル・アイコン・確認の取り方）/ `data_flow.md`（WS 契約・受信境界・判定の置き場所）/ `pitfalls.md`（踏んだ罠とテスト）。**UI を触るときはここから読む** |
| [`docs/venue_recovery.md`](docs/venue_recovery.md) | **会場カード。試合当日に手が止まったときはこれ 1 枚** |
| [`docs/mechanism_handoff.md`](docs/mechanism_handoff.md) | 機構が付いた日に埋める値の棚卸し。機構担当と共有する表 |
| [`docs/operations.md`](docs/operations.md) | コマンド・CAN セットアップ・systemd 運用 |
| [`firmware/README.md`](firmware/README.md) / [`web/README.md`](web/README.md) | 各サブプロジェクトの実務 |
| [`docs/todo.md`](docs/todo.md) | **未着手と未決の一覧。** 実装まで済んだら該当文書へ移してここからは消す |
| [`docs/history/`](docs/history/) | いつ何が起きたかの記録。**正ではない。参照して実装を決めない** |

**`architecture.md` と `invariants.md` は対。構造は前者、その構造を崩してはならない理由は
後者にあり、同じ話を両方には書かない。** 文書を書き足すときもこの境界を守ること。

## 作業の前に読むもの

**コードを変える前に、触る領域に対応する `docs/invariants.md` の節を必ず読むこと。**
そこにあるのは「実機で一度壊れた」記録であり、読まずに直すと同じ壊れ方を繰り返す。

| 触るもの | 読む節 |
|---|---|
| `lib/can_manager.py`, `lib/drivers/` | §1 CAN バスとフレーム |
| `lib/control/`, `lib/axis_sync.py`, `lib/motion_guard.py` | §2 制御ループと軸 |
| 緊急停止・ホーミング・再励磁・後始末 | §3 安全機構 |
| `lib/manual.py`, `sequences/`, 動作確認, 指差喚呼 | §4 運用と操作モード |
| `config/`, `lib/config_schema.py`, `main.py`, ログ | §5 設定と起動 |
| `lib/server.py`, `lib/commands.py`, `lib/ws_hub.py` | §6 サーバーと配信 |
| `firmware/` | §7 ファームウェア + `docs/motor_driver_can_protocol.md` |
| `web/src/` | §8 Web UI + [`docs/web/`](docs/web/)（画面・部品・データフローの現況） |
| `tests/`, `web/src/**/*.test.*`, `firmware/test/` | §9 テスト |

## コマンド

```bash
uv run python main.py             # サーバー起動（localhost:8080）
uv run python main.py --dry-run   # CAN バスなしで起動
uv run pytest                     # Python 側の全テスト
uv run ruff check . && uv run ruff format .
cd web && pnpm check              # lint + format + 型検査 + テスト + ビルド
pio test -e native -d firmware/servo  # ファームの native テスト（実機不要）
```

**変更したら、その領域のテストを必ず通すこと。** 全体像・ビルド・書き込み・systemd 運用は
[`docs/operations.md`](docs/operations.md)。

## 設定ファイルの分担

| ファイル | 持つもの |
|---|---|
| `config/system.yaml` | PC 上に 1 つしか存在しない設定。バス別名・`health`・`match` |
| `config/can_buses.yaml` | CAN バス定義の単一情報源。udev ルールとセットアップスクリプトの双方が参照する |
| `config/<robot>.yaml` | そのロボットのモータ構成（ドライバ種別・バス別名・CAN ID・PID） |
| `config/<robot>_positions.yaml` | 論理軸の単位換算・機構位置の定数・手動操縦の可動範囲 (`manual`)・機械的可動域 (`travel`) |
| `config/checklist.yaml` | セッティングタイムの指差喚呼チェックリスト |
| `config/bench/<対象>/` | 机上ベンチ用の一式（8 セット） |

読み込みと検証は `lib/config_schema.py` に一本化してある。

## 常に効く約束

文書を開かなくても守るべきメタ規則。個別の理由は `docs/invariants.md` にある。

- **同じ判定を 2 箇所に書かない。** 単一情報源が決まっているものは、それを呼ぶ
  （コマンド語彙は `lib/commands.py`、偏差判定は `lib/axis_sync.py`、ヘルス集約は
  `lib/health.py` と `web/src/lib/healthVerdict.ts`、しきい値の既定値は `lib/config_schema.py`）
- **`sequences/*.py` に数値を書かない。** 位置名で書き、値は位置定数 yaml が持つ
- **モータ名と CAN ID はロボット横断に一意。** `tests/test_robot_sequences.py` が固定している
- **UI にモータ名・ドライバ種別を書き写さない。** サーバーが判定して配る
- **測れない値を 0 で埋めない。** 「測る手段が無い」は `null`、「読めなかった」は `MALFORMED`
- **黙った既定値（`?? []`、`0.0`）で欠落を埋めない。** 埋めたこと自体が画面から読めなくなる
- **止める処理・後始末は、1 つ失敗しても残りを続ける形にする**
- **テストを足したら、本番コードにわざと 1 行の不具合を入れて、狙ったテストが落ちることを
  確認してから元に戻す。** 落ちなければ実装の存在しか見ていない
- **プロトコルかピン配置を変えたら、ファームの `kFirmwareVersion` と `config/**/*.yaml` の
  `expected_firmware` を同じコミットで揃える**
- **設計を変えたら、対応する文書（`invariants.md` / `architecture.md`）も同じコミットで直す**

## テスト方針

TDD でプロトコル層とシーケンスエンジンを開発する。テストを先に書き（RED）、実装して通す
（GREEN）。**安全に直結する不変条件を触ったときは、多重防護の各層を 1 枚ずつ単独で確かめる**
（統合経路のテストでは 1 枚壊しても他が拾って落ちない）。詳細は `docs/invariants.md` §9 と
`docs/architecture.md` のテスト戦略。

## コメントの書き方

**なぜその実装が必要かだけを書く。** 変更の経緯（「かつては」「以前は」「実際に一度」）は
コメントに書かず、必要なら `docs/invariants.md` か `docs/history/` へ置いて参照する。
自明な処理の説明と抽象的すぎる説明は書かない。

## 言語

日本語でコミュニケーションすること。コード中のコメントも日本語で可。
