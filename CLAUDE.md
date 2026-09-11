# CLAUDE.md

Claude Code（claude.ai/code）がこのリポジトリで作業するときの指針。

## 最優先方針 — スピード

**大会本番期。動くプログラムを速く出すことが最優先。**
文書・テスト・コメントの整備より、実装と実機での動作確認を先に進めること。

- 実装を最優先。長い計画書の作成や網羅的な事前調査より、まず動かして直す
- **テストはクリティカルな部分にしか書かない**（下に定義）。それ以外は書かない
- 文書の更新は求められたときだけ。設計変更に文書を追従させる義務はない
- コメントは最小限。書くなら 1 行で「なぜ」だけ
- 説明・前置きは短く。判断できるものは確認待ちで止まらず進める

**例外は安全機構だけ。** 緊急停止・ホーミング・貫通防止・軸間干渉を触るときは、
`docs/invariants.md` の該当節を読んでから直すこと。ここは壊れると実機と人が危なく、
本番中の復旧に一番時間を食う。

## テストを書いてよい範囲

**新しいテストを作るのは、下の「クリティカル」に当たるときだけ。それ以外は書かない。**
UI の見た目、ログ、ツール、設計の作法、文言の照合には**テストを作らないこと**。

クリティカル（ここだけ書く）:

- **止める仕組み** —— 緊急停止、可動端、貫通防止、軸間干渉、再励磁、後始末
- **原点** —— ホーミング、零点確定、原点を失ったあとの復帰
- **機体を動かす数値** —— 位置定数、シーケンスのステップ、可動範囲の検証
- **CAN とファーム** —— フレームの組み立てと解釈、焼き忘れ検出、途絶の判定
- **バグると機体が壊れる制御** —— PID、軌道、同期ずれ

書かない（消してよい）:

- コンポーネントが描画されるかどうかだけを見る UI テスト
  （緊急停止の表示は例外として残してある）
- ログ設定、チューニングツール、ID 書き換えツール
- 内部構造の作法を見る設計検査
- 文書やチェックリストの文言・数値の照合

**クリティカルに当たっても、既存のテストで守られている範囲には足さない。** 実際に書きすぎている。
**迷ったら書かない。**

**既存のテストが落ちたら、消さずに原因を見ること。** 落ちたテストは上のどれかを
守っている。

## プロジェクト概要

キャチロボバトルコンテスト 2026 出場ロボットの中央制御プログラム。固定型ロボット
（メインハンド + サブハンド）を半自動シーケンス制御で動作させる。同一 PC 上で両ロボットを
制御し、Web UI（localhost:8080）から操縦者が操作する。自作モータドライバのファームウェア
（PlatformIO / CubeMX）も同じリポジトリに同居している。

asyncio 単一プロセスで CAN 通信・シーケンス制御・Web サーバーを統合実行する。

## 残っている文書

| 文書 | 何の正か |
|---|---|
| [`docs/venue_recovery.md`](docs/venue_recovery.md) | **会場カード。試合当日に手が止まったときはこれ 1 枚** |
| [`docs/invariants.md`](docs/invariants.md) | **崩してはならない設計と、その理由。** 実機で一度壊れた記録 |
| [`docs/operations.md`](docs/operations.md) | コマンド・CAN セットアップ・systemd 運用 |
| [`docs/motor_driver_can_protocol.md`](docs/motor_driver_can_protocol.md) | 自作モータドライバ CAN プロトコル。`lib/drivers/generic.py` と `firmware/` の双方がこれに従う |

## 安全機構を触るときに読む節

| 触るもの | 読む節 |
|---|---|
| 緊急停止・ホーミング・再励磁・後始末 | `docs/invariants.md` §3 |
| `lib/control/`, `lib/axis_sync.py`, `lib/motion_guard.py` | §2 制御ループと軸 |
| 軸間干渉・可動端・シーケンスの並び | §4 運用と操作モード |
| `lib/can_manager.py`, `lib/drivers/` | §1 CAN バスとフレーム |
| `firmware/` | §7 + `docs/motor_driver_can_protocol.md` |

## コマンド

```bash
uv run python main.py             # サーバー起動（localhost:8080）
uv run python main.py --dry-run   # CAN バスなしで起動
uv run pytest                     # Python 側のテスト（並列 + slow 除外で約 9 秒）
uv run pytest -m ""               # slow も含めた全件（CI と同じ）
uv run ruff check . && uv run ruff format .
cd web && pnpm check              # lint + format + 型検査 + テスト + ビルド
pio test -e native -d firmware/servo  # ファームの native テスト（実機不要）
scripts/deploy.sh                 # 実機へ反映: pull + 依存導入 + UI ビルド + 全サービス再起動
```

## 実機のプログラムを再起動・反映するとき

- サービスが参照する本体チェックアウト（`systemctl show cbc-control -p WorkingDirectory`、
  現状 `/home/drc/cbc2026_team3`）で `scripts/deploy.sh` を実行する。worktree の deploy.sh はガードで止まる
- deploy.sh は upstream から pull する。反映したい変更は先に本体チェックアウトのブランチの upstream へ push しておく
- `main.py` を `nohup` / `setsid` で手動常駐させない（8080 を握り、cbc-control が起動のたびに落ちる）。
  `--dry-run` や机上ベンチの一時起動は `timeout` 付きで
- 再起動で UI 接続は全部切れ、CAN も全バス down/up する。操作中の人がいないか確かめてから
- 会場（ネットワークなし）では `--no-install`

## 設定ファイルの分担

| ファイル | 持つもの |
|---|---|
| `config/system.yaml` | PC 上に 1 つしか存在しない設定。バス別名・`health`・`match` |
| `config/can_buses.yaml` | CAN バス定義の単一情報源。udev ルールとセットアップスクリプトの双方が参照する |
| `config/<robot>.yaml` | そのロボットのモータ構成（ドライバ種別・バス別名・CAN ID・PID） |
| `config/<robot>_positions.yaml` | 論理軸の単位換算・機構位置の定数・手動操縦の可動範囲 (`manual`)・機械的可動域 (`travel`) |
| `config/bench/<対象>/` | 机上ベンチ用の一式 |

読み込みと検証は `lib/config_schema.py` に一本化してある。

## 壊さないための最低限

- **同じ判定を 2 箇所に書かない。** 単一情報源が決まっているものは、それを呼ぶ
  （コマンド語彙は `lib/commands.py`、偏差判定は `lib/axis_sync.py`、ヘルス集約は
  `lib/health.py` と `web/src/lib/healthVerdict.ts`、しきい値の既定値は `lib/config_schema.py`）
- **同じ問題に片方のロボット専用の仕組みを新設しない。** メインとサブは同じ仕組みで守る
  （軸間干渉なら `guard.requires` / `guard.not_with` / `interlocks:`）。専用キーを足すと
  直すときに 2 箇所を追うことになる
- **`sequences/*.py` に数値を書かない。** 位置名で書き、値は位置定数 yaml が持つ
- **モータ名と CAN ID はロボット横断に一意**
- **UI にモータ名・ドライバ種別を書き写さない。** サーバーが判定して配る
- **測れない値を 0 で埋めない。** 「測る手段が無い」は `null`、「読めなかった」は `MALFORMED`
- **止める処理・後始末は、1 つ失敗しても残りを続ける形にする**
- **プロトコルかピン配置を変えたら、ファームの `kFirmwareVersion` と `config/**/*.yaml` の
  `expected_firmware` を同じコミットで揃える**

## 言語

日本語でコミュニケーションすること。コード中のコメントも日本語で可。
