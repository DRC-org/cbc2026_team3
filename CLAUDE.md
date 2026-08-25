# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## プロジェクト概要

キャチロボバトルコンテスト 2026 出場ロボットの中央制御プログラム。
固定型ロボット（メインハンド + サブハンド）を半自動シーケンス制御で動作させる。
同一 PC 上で両ロボットを制御し、Web UI（localhost:8080）から操縦者が操作する。
自作モータドライバのファームウェア（PlatformIO）も同じリポジトリに同居している。

## 設計文書

| 文書 | 役割 |
|---|---|
| `docs/impl_plan.md` | 実装計画・設計判断の記録。**すべての実装判断はこれに従う。設計変更・追加作業を行ったら必ず更新すること** |
| `docs/motor_driver_can_protocol.md` | 自作モータドライバ CAN プロトコルの**単一情報源**。PC 側 `lib/drivers/generic.py` と `firmware/` の双方がこれに従う |

## コマンド

### Python バックエンド（uv 管理）

```bash
uv run python main.py             # サーバー起動（localhost:8080）
uv run python main.py --dry-run   # CAN バスなしで起動（virtual バス。配線確認に使える）
uv run pytest                     # 全テスト実行
uv run pytest tests/drivers/      # ドライバテストのみ
uv run pytest -x                  # 最初の失敗で停止
uv run pytest -k "m3508"          # 特定テストのみ
uv run ruff check .               # リント
uv run ruff format .              # フォーマット
```

### Web UI（web/ ディレクトリ）

```bash
cd web && pnpm install            # 依存インストール
cd web && pnpm dev                # 開発サーバー起動
cd web && pnpm build              # プロダクションビルド
cd web && pnpm test               # vitest（watch）
cd web && pnpm test:run           # vitest（1 回だけ実行）
cd web && pnpm check              # lint + format + 型検査 + テスト
```

### ファームウェア（PlatformIO）

`-d` にプロジェクトディレクトリを渡せばリポジトリ直下から実行できる。

```bash
pio test -e native -d firmware/dc_motor   # 実機不要。プロトコル層・安全機構・PID
pio test -e native -d firmware/servo      # 実機不要。角度補間・可動範囲クランプ・到達推定
pio run -e uno_r4_minima -d firmware/dc_motor
pio run -e uno_r4_minima -d firmware/servo -t upload
```

`firmware/lib/MotorCan/` は両ファームで共有しているので、**触ったら両方の native テストを回すこと。**

### CAN セットアップ

```bash
sudo scripts/install.sh           # udev ルール配置 + systemd 有効化（初回のみ）
scripts/setup_can.sh              # 手動 up。見つかったバスだけ立ち上げる（開発用）
scripts/setup_can.sh --strict     # 試合前点検。3 本揃わなければ異常終了

# vcan（CAN 統合テスト用）
sudo modprobe vcan
sudo ip link add dev vcan0 type vcan
sudo ip link set up vcan0
```

## アーキテクチャ

asyncio 単一プロセスで CAN 通信・シーケンス制御・Web サーバーを統合実行する。

- `lib/` — 共通ライブラリ（両ロボットで共有）
  - `can_manager.py` — SocketCAN 複数バス管理。受信ループがモータの `matches_feedback` でフレームを振り分ける
  - `drivers/` — モータドライバ群（M3508 / EDULITE 05 / 自作モタドラ）。`base.py` の基底クラスを継承
  - `control/position_loop.py` — M3508 の PC 側位置制御 PID ループ（200Hz）
  - `control/sync_monitor.py` — 左右ペア軸のずれを常時監視（50Hz）。超過で全体緊急停止
  - `sequence/positions.py` — 位置定数 yaml の読み込み・単位換算・論理軸の解決
  - `sequence/motors.py` — `MotorHandle`（1 モータ）と `AxisHandle`（1 論理軸 = 1〜N モータ）
  - `sequence/engine.py` — `@step` デコレータベースのシーケンスエンジン。`require_trigger=True` で操縦者の許可待ち
  - `motor_check.py` — セッティングタイムのアクチュエータ動作確認
  - `server.py` — aiohttp で HTTP 静的配信 + WebSocket (`/ws`) を統合
- `robots/` — ロボット固有のシーケンス定義（main_hand.py / sub_hand.py）
- `config/` — YAML 設定（後述）
- `firmware/` — 自作モータドライバのファームウェア（PlatformIO / Arduino UNO R4）
- `web/` — Vite + React + TypeScript の操作 UI（画面切替はタブ + URL ハッシュ。ルーターは使わない）
  - `src/test/` — vitest 共通ヘルパ（WebSocket スタブ、RobotProvider ラッパ）。テスト本体は対象ソースの隣に `*.test.ts(x)`

### 設定ファイルの分担

| ファイル | 持つもの |
|---|---|
| `config/can_buses.yaml` | CAN バス定義の単一情報源。udev ルールとセットアップスクリプトの双方がここから生成・参照する |
| `config/<robot>.yaml` | モータ構成（ドライバ種別・バス・CAN ID・PID・動作確認設定） |
| `config/<robot>_positions.yaml` | 論理軸の単位換算と機構位置の定数 |
| `config/checklist.yaml` | セッティングタイムの指差喚呼チェックリスト |

**`robots/*.py` に数値を書いてはならない。** シーケンスは `move_to({"軸名": "位置名"})` の形で書き、
単位換算・許容差・待ち時間はすべて位置定数 yaml が持つ。機構が変わったら yaml の数値だけを差し替える。

### 知っておくべき設計上の制約

**CAN バス名は udev で個体固定する。** `can0`/`can1`/`can2` は USB 列挙順で入れ替わり、
番号が入れ替わると C620 に EDULITE 用のコマンドが飛んでモータを壊す。
バス名は `can_m3508` / `can_edulite` / `can_generic` で、STM32 UID 由来の serial に紐付ける。

**CAN ID はバス単位でロボット横断に一意。** メインハンドとサブハンドは物理的に同じ
`can_edulite` / `can_generic` を共有する。重複すると受信ループが最初にマッチした 1 台で
打ち切るため、もう一方は永久にフィードバックを得られない。`tests/test_robot_sequences.py` で検証している。

**M3508 は電流指令しか受け付けない。** 位置制御は PC 側の PID ループが担う。
C620 の電流指令フレーム（`0x200`）は 1 通に 4 モータ分のスロットを持つため、
**同一バス上の M3508 は必ず 1 つの `M3508PositionLoop` が束ねる**（個別送信すると他モータのスロットを 0 で潰す）。

**左右ペア軸は 3 重に保護している。** `y_axis`（M3508 ×2）と `rotate`（EDULITE ×2）は機構的に直結し、
位置がずれるとその場で壊れる。位置定数 yaml の `motors:` で 1 論理軸に複数モータを束ね、
逆回転は `scale` の符号で表す。保護は ①同一フレームでの同時指令 ②偏差監視（ループ内 200Hz で電流 0 /
`SyncMonitor` 50Hz で全体緊急停止）③フィードバック途絶をペア単位で判定（片方だけ止めると残った側が押し続ける）。

**離散状態アクチュエータは新ドライバを作らず位置定数で表す。** グリッパの開/閉、壁の初期/閉/開は
`positions` の名前付き状態として書く。`move_to` は位置名でしか値を引けないため、
定義した状態以外を送れないことが構造的に保証される。

**ファームと PC 側はプロトコルの対。** `docs/motor_driver_can_protocol.md` を単一情報源とし、
片方だけを変更してはならない。`firmware/lib/MotorCan/` が `Arduino.h` を include しないのは、
native 環境（`pio test -e native`）でプロトコル層と安全機構をテストできるようにするため。

**UNO R4 は基板ごとに CAN のピンが違う。** Minima が `D4`(TX)/`D5`(RX)、WiFi が `D10`/`D13`。
DC 基板は配線が Minima 前提のため `uno_r4_wifi` の env を意図的に置いていない。
各 `main.cpp` の `static_assert` がピン衝突をビルド時に検出する。

**Web UI はモータ名をハードコードしていない。** モータ状態は `Record<string, MotorState>` として
そのまま流れるので、モータの増減で UI 側の変更は要らない。

## テスト方針

TDD でプロトコル層とシーケンスエンジンを開発する。テストを先に書き（RED）、実装して通す（GREEN）。
詳細は `docs/impl_plan.md` の「テスト戦略」セクションを参照。

## 言語

日本語でコミュニケーションすること。コード中のコメントも日本語で可。
