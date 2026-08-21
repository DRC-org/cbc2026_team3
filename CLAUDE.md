# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## プロジェクト概要

キャチロボバトルコンテスト 2026 出場ロボットの中央制御プログラム。
固定型ロボット（メインハンド + サブハンド）を半自動シーケンス制御で動作させる。
同一 PC 上で両ロボットを制御し、Web UI（localhost:8080）から操縦者が操作する。

## 実装方針

**すべての実装判断は `impl_plan.md` に従うこと。** 設計変更・追加作業を行った場合は必ず `impl_plan.md` を更新すること。

## コマンド

### Python バックエンド（uv 管理）

```bash
uv run python main.py            # サーバー起動（localhost:8080）
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

### vcan セットアップ（CAN 統合テスト用）

```bash
sudo modprobe vcan
sudo ip link add dev vcan0 type vcan
sudo ip link set up vcan0
```

## アーキテクチャ

asyncio 単一プロセスで CAN 通信・シーケンス制御・Web サーバーを統合実行する。

- `lib/` — 共通ライブラリ（両ロボットで共有）
  - `can_manager.py` — SocketCAN 複数バス管理（can0/can1/can2）
  - `drivers/` — モータドライバ群（M3508, EDULITE 05, 自作モタドラ）。`base.py` の基底クラスを継承
  - `sequence/engine.py` — `@step` デコレータベースのシーケンスエンジン。`require_trigger=True` で操縦者の許可待ち
  - `server.py` — aiohttp で HTTP 静的配信 + WebSocket (`/ws`) を統合
- `robots/` — ロボット固有のシーケンス定義（main_hand.py / sub_hand.py）
- `config/` — YAML でモータ構成を宣言（ドライバ種別、CAN バス、CAN ID）
- `web/` — Vite + React + TypeScript の操作 UI（画面切替はタブ + URL ハッシュ。ルーターは使わない）
  - `src/test/` — vitest 共通ヘルパ（WebSocket スタブ、RobotProvider ラッパ）。テスト本体は対象ソースの隣に `*.test.ts(x)`

CAN バスは 3 系統: can0（M3508）、can1（EDULITE 05）、can2（自作モタドラ）。
プロトコルが異なる（M3508=Standard Frame, EDULITE 05=Extended Frame 29bit, 自作=Standard Frame）。

## テスト方針

TDD でプロトコル層とシーケンスエンジンを開発する。テストを先に書き（RED）、実装して通す（GREEN）。
詳細は `impl_plan.md` の「テスト戦略」セクションを参照。

## 言語

日本語でコミュニケーションすること。コード中のコメントも日本語で可。
