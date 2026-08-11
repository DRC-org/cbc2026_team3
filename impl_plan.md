# CBC2026 Team3 中央制御プログラム 実装計画

## 概要

キャチロボバトルコンテスト 2026 に出場するロボットの中央制御プログラム。
固定型ロボットにメインハンドとサブハンドがあり、それぞれ半自動シーケンス制御で動作する。

### 技術スタック

- **バックエンド**: Python 3.12+ / asyncio（単一プロセス）
- **CAN 通信**: python-can + SocketCAN
- **Web UI**: Vite + React + React Router + TypeScript
- **通信**: WebSocket（JSON）
- **サーバー**: aiohttp（HTTP 静的配信 + WebSocket を統合）

### 開発ツールチェーン

| ツール | 用途 |
|---|---|
| **uv** | パッケージマネージャ・仮想環境管理（pip/venv の代替） |
| **ruff** | リンター + フォーマッター（flake8/black/isort の代替） |
| **pytest** | テストフレームワーク |
| **pytest-asyncio** | asyncio テスト対応 |
| **mypy** | 型チェック（任意、余裕があれば） |

#### uv の使い方

```bash
# プロジェクト初期化（pyproject.toml ベース）
uv init

# 依存追加
uv add python-can aiohttp pyyaml
uv add --dev pytest pytest-asyncio ruff

# 仮想環境での実行
uv run python main.py
uv run pytest

# ruff
uv run ruff check .       # リント
uv run ruff format .      # フォーマット
```

### 設計判断

- **ROS 2 不採用**: 固定型 + 一本道シーケンスでは DDS のメリットが薄く、WebSocket 通信との統合で不要な複雑性が生じるため
- **Python メイン**: シーケンス制御の記述性を優先。モータドライバ側で PID が閉じており、中央 PC からは目標値送信のみなので asyncio で十分なリアルタイム性を確保できる
- **aiohttp 採用**: 静的ファイル配信と WebSocket を 1 プロセスで統合でき、localhost:8080 で全機能を提供可能

---

## ハードウェア構成

### モータ

| モータ | 個数 | ESC/ドライバ | CAN プロトコル |
|---|---|---|---|
| DJI M3508 | 2 | C620 ESC | CAN 2.0A Standard Frame |
| RobStride EDULITE 05 | 2 | 内蔵 | CAN 2.0B Extended Frame (29bit) |
| DC モータ / サーボ | 多数 | 自作モータドライバ | CAN 2.0A Standard Frame（後述） |

### CAN バス構成（3 系統）

| バス | USB-CAN アダプタ | 接続デバイス |
|---|---|---|
| can0 | CANable #1 | M3508 × 2 |
| can1 | CANable #2 | EDULITE 05 × 2 |
| can2 | CANable #3 | DC モータ / サーボ（自作モタドラ） |

---

## 自作モータドライバ用 CAN プロトコル

CAN 2.0A Standard Frame（11bit ID）を使用。RobStride の Extended Frame と衝突しない。

### CAN ID レイアウト（11bit）

```
Bit10~8 (3bit): コマンド種別
Bit7~0  (8bit): デバイスID (0x01~0xFE)
```

| コマンド種別 | 値 | 方向 | 説明 |
|---|---|---|---|
| SET_TARGET | 0b000 | PC → モタドラ | 目標値設定 |
| FEEDBACK | 0b001 | モタドラ → PC | 状態フィードバック |
| SET_MODE | 0b010 | PC → モタドラ | 動作モード変更 |
| SET_PARAM | 0b011 | PC → モタドラ | パラメータ変更（PID ゲイン等） |
| E_STOP | 0b111 | PC → モタドラ | 緊急停止（デバイス ID=0xFF で全体停止） |

### データフレーム定義

**SET_TARGET（目標値設定）**

```
Byte 0:    制御タイプ (0=position, 1=velocity, 2=duty)
Byte 1:    予約
Byte 2-5:  目標値 (float32, little-endian)
Byte 6-7:  予約
```

**FEEDBACK（フィードバック）**

```
Byte 0-1:  現在位置 (int16, 0.1deg 単位 or エンコーダ値)
Byte 2-3:  現在速度 (int16, rpm)
Byte 4-5:  電流 (int16, mA)
Byte 6:    温度 (uint8, ℃)
Byte 7:    状態フラグ (bit0=到達, bit1=過電流, bit2=過熱)
```

**E_STOP（緊急停止）**

CAN ID = `0x7FF`（コマンド種別=0b111, デバイスID=0xFF）、データ不要、全デバイスが受信して即停止。

---

## アーキテクチャ

```
┌──────────────────────────────────────┐
│         Web UI (React + Vite)        │
│   localhost:8080                      │
│   ボタン: [次へ] [緊急停止] [状態表示]   │
└──────────────┬───────────────────────┘
               │ WebSocket (localhost:8080/ws)
┌──────────────▼───────────────────────┐
│      Central Controller (Python)     │
│      asyncio ベースの単一プロセス       │
│                                      │
│  ┌────────────┐  ┌────────────────┐  │
│  │ aiohttp    │  │  Sequence      │  │
│  │ Server     │  │  Engine        │  │
│  │ HTTP + WS  │  │  (FSM)         │  │
│  └─────┬──────┘  └───────┬────────┘  │
│        │                 │           │
│  ┌─────▼─────────────────▼────────┐  │
│  │       CAN Manager              │  │
│  │  ┌─────────┐ ┌──────────────┐  │  │
│  │  │ M3508   │ │ EDULITE 05   │  │  │
│  │  │ Driver  │ │ Driver       │  │  │
│  │  │(can0)   │ │(can1)        │  │  │
│  │  └────┬────┘ └──────┬───────┘  │  │
│  │  ┌────┴─────────────┴───────┐  │  │
│  │  │ Generic Driver (can2)    │  │  │
│  │  └──────────┬───────────────┘  │  │
│  └─────────────┼──────────────────┘  │
└────────────────┼─────────────────────┘
                 │ SocketCAN
    ┌────────────┼────────────┐
 [can0]       [can1]       [can2]
 USB-CAN#1   USB-CAN#2   USB-CAN#3
    │            │            │
[M3508×2] [EDULITE05×2] [DC/Servo...]
```

---

## テスト戦略

### 方針: プロトコル層とシーケンスエンジンを TDD で開発

実機デバッグで時間が溶けやすいバイト列の組み立てミスや状態遷移のバグを、テストで先に潰す。

### テスト対象とアプローチ

| レイヤー | TDD | テスト手法 |
|---|---|---|
| **M3508 プロトコル** | ◎ | エンコード/デコードの単体テスト。期待するバイト列との比較 |
| **EDULITE 05 プロトコル** | ◎ | 29bit CAN ID 組み立て・パース、値マッピングの単体テスト |
| **自作モタドラプロトコル** | ◎ | エンコード/デコードの単体テスト |
| **シーケンスエンジン** | ◎ | モータドライバを mock し、ステップ遷移・trigger 待ち・エラー処理をテスト |
| **config パース** | ○ | YAML → ドライバインスタンス生成の単体テスト |
| **CAN 実通信** | △ | vcan（仮想 CAN）を使った統合テスト。CI でも実行可能 |
| **WebSocket プロトコル** | ○ | JSON パース/生成の単体テスト |
| **aiohttp サーバー** | △ | aiohttp.test_utils で最低限の結合テスト |
| **Web UI (React)** | - | 今回はスコープ外 |

### vcan を使った統合テスト

```bash
# vcan セットアップ（テスト実行前に 1 回だけ）
sudo modprobe vcan
sudo ip link add dev vcan0 type vcan
sudo ip link set up vcan0
```

pytest の fixture で vcan バスを自動セットアップし、実際の CAN フレーム送受信をテストする。

### テストファイル構成

```
tests/
├── conftest.py                  # 共通 fixture（mock モータ、vcan セットアップ等）
├── drivers/
│   ├── test_m3508.py            # M3508 エンコード/デコード
│   ├── test_edulite05.py        # EDULITE 05 エンコード/デコード
│   └── test_generic.py          # 自作プロトコル エンコード/デコード
├── test_sequence_engine.py      # シーケンスエンジンの状態遷移
├── test_can_manager.py          # vcan を使った統合テスト
└── test_ws_protocol.py          # WebSocket JSON プロトコル
```

### TDD の流れ（各ドライバ実装時）

1. プロトコル仕様からテストケースを先に書く（期待するバイト列、変換値）
2. テストが RED になることを確認
3. ドライバを実装して GREEN にする
4. ruff format + ruff check でコード品質を確認

### テスト実行

```bash
uv run pytest                    # 全テスト実行
uv run pytest tests/drivers/     # ドライバテストのみ
uv run pytest -x                 # 最初の失敗で停止
uv run pytest -k "m3508"         # M3508 関連のみ
```

---

## ディレクトリ構成

```
cbc2026_team3_central/
├── pyproject.toml
├── config/
│   ├── main_hand.yaml
│   ├── sub_hand.yaml
│   └── checklist.yaml      # 指差喚呼チェックリスト定義
├── lib/
│   ├── __init__.py
│   ├── can_manager.py
│   ├── match_state.py      # モード / コート / フェーズ / チェックリスト
│   ├── drivers/
│   │   ├── __init__.py
│   │   ├── base.py
│   │   ├── m3508.py
│   │   ├── edulite05.py
│   │   └── generic.py
│   ├── sequence/
│   │   ├── __init__.py
│   │   └── engine.py
│   └── server.py
├── robots/
│   ├── __init__.py
│   ├── main_hand.py
│   └── sub_hand.py
├── web/
│   ├── package.json
│   ├── tsconfig.json
│   ├── vite.config.ts
│   ├── index.html
│   └── src/
│       ├── main.tsx
│       ├── App.tsx
│       ├── router.tsx
│       ├── hooks/
│       │   └── useRobotSocket.ts
│       ├── pages/
│       │   ├── Dashboard.tsx
│       │   ├── RobotControl.tsx
│       │   └── MotorTuning.tsx
│       └── components/
│           ├── SequenceProgress.tsx
│           ├── MotorStatus.tsx
│           ├── TriggerButton.tsx
│           ├── Checklist.tsx       # 指差喚呼チェックリスト
│           ├── MatchControl.tsx    # モード/コート切替 + 試合開始・終了
│           ├── PhaseBanner.tsx     # MODE/COURT/PHASE 常時表示
│           └── EStopButton.tsx
├── main.py
└── tests/
    ├── conftest.py
    ├── drivers/
    │   ├── test_m3508.py
    │   ├── test_edulite05.py
    │   └── test_generic.py
    ├── test_sequence_engine.py
    ├── test_sequence_court_auto.py
    ├── test_match_state.py
    ├── test_server_match.py
    ├── test_can_manager.py
    └── test_ws_protocol.py
```

---

## WebSocket プロトコル（JSON）

### Server → Client（状態配信）

```jsonc
{
  "type": "state",
  "robot": "main_hand",
  "sequence": "pick_and_place",
  "current_step": "extend_arm",
  "step_index": 1,
  "total_steps": 5,
  "waiting_trigger": true,
  "steps": [
    { "index": 0, "label": "初期位置へ移動", "require_trigger": false, "auto_stop": false },
    { "index": 1, "label": "ワーク前まで前進", "require_trigger": true, "auto_stop": false }
  ],
  "motors": {
    "m3508_1": { "pos": 1500, "vel": 0.0, "torque": 0.2, "temp": 35.0 },
    "edulite_1": { "pos": 0.5, "vel": 0.0, "torque": 0.1, "temp": 28.0 }
  },
  "e_stop_active": false,
  "health": { /* HealthSnapshot */ }
}
```

### Client → Server（操作）

```jsonc
{ "type": "trigger", "robot": "main_hand" }
{ "type": "sequence_jump", "robot": "main_hand", "step_index": 3 }
{ "type": "sequence_stop", "robot": "main_hand" }
{ "type": "sequence_start", "robot": "main_hand" }
{ "type": "e_stop" }
{ "type": "e_stop_release" }
{ "type": "set_param", "motor": "m3508_1", "key": "kp", "value": 1.5 }
```

### シーケンス制御コマンドのセマンティクス

- **trigger**: `require_trigger=true` のステップを次に進める
- **sequence_jump**: 任意のステップへジャンプ。実行中なら次のステップ境界で反映、停止中・完走後なら指定 index から再開
- **sequence_stop**: 通常停止 (緊急停止と異なり CAN 層には介入しない)。停止後 `step_index=0` に戻り `running=false` になる
- **sequence_start**: 先頭から実行開始 (停止後・完走後の再起動)

**シーケンスは起動時に自動実行しない。** `_run_sequence_loop` は resume 要求を待って停止したまま起動し、
操縦者の明示的な `sequence_start` (半自動) または `match_start` (全自動) があるまでロボットを動かさない。

### dry-run モード

`uv run python main.py --dry-run` 起動時、`RobotServer` は以下の変更を加えて Web UI を完全デモ可能にする:

- 各モータの状態は `time.time()` ベースのサイン波で擬似生成（pos/vel/torque/temp）
- ヘルススナップショットは全モータ・全バスを `ok` に上書き（virtual バスではフィードバックが返らないため）
- シーケンスタスクは `_on_startup` で起動されるが、実機同様に開始合図を待って停止したままになる

---

## 試合運用フロー（モード / コート / フェーズ）

`lib/match_state.py` が試合全体の状態を一元管理する。ロボット単位の `state` メッセージとは別系統。
操縦者 2 名 + Monitor が**別ブラウザ**で接続するため、チェックリストの進捗をクライアント側に持つと
「2 人とも完了」の判定ができない。**正は必ずサーバー側**に置く。

### 2 つの直交する軸

| 軸 | 値 | 意味 |
|---|---|---|
| `mode` | `semi_auto` / `full_auto` | 半自動は操縦者 2 名、全自動は Monitor 1 名で運用 |
| `court` | `red` / `blue` | 自陣コート。赤青で配置が左右反転する |
| `phase` | `setup` → `ready` → `match` → `finished` | セッティングタイムと試合中を分離 |

### フェーズ遷移

```
setup ⇄ ready → match → finished → setup
  ↑ 必要チェックリスト完了で自動遷移 (ready)、チェックが外れると setup に戻る
              ↑ match_start (明示操作のみ)
                      ↑ match_finish   ↑ match_reset (どのフェーズからでも可)
```

- 必要ロール: `semi_auto` → `main_hand` + `sub_hand` / `full_auto` → `monitor`
- `mode` / `court` を変更するとチェックリストは**全リセット**され `setup` に戻る（配置が変わるため指差喚呼をやり直す）
- `match_reset` はモード・コートを維持したままチェックリストのみリセットする
- 項目ゼロのロールは「完了」とみなす（ゲートが永久に開かなくなるのを防ぐ）

### フェーズによるコマンドゲート

`MatchState.deny_reason(command)` が単一の判定点。UI でボタンを隠すだけでは WS 直叩きや
リロード直後を防げないため、サーバー側でも同じ制約を掛ける。

| コマンド | setup | ready | match | finished |
|---|:-:|:-:|:-:|:-:|
| `set_mode` / `set_court` | ✓ | ✓ | ✗ | ✓ |
| `checklist_set` / `checklist_reset` | ✓ | ✓ | ✗ | ✗ |
| `motor_check_start` | ✓ | ✓ | ✗ | ✓ |
| `match_start` | ✗ | ✓ | ✗ | ✗ |
| `match_finish` | ✗ | ✗ | ✓ | ✗ |
| `sequence_start` / `sequence_jump` / `trigger` | ✗ | ✗ | ✓ | ✗ |
| `sequence_stop` / `e_stop` / `match_reset` | ✓ | ✓ | ✓ | ✓ |

拒否時は `{"type":"command_rejected","command":...,"reason":...}` を配信する。
ただし `motor_check_start` の拒否だけは既存の `motor_check_error` イベントに合わせる（UI の表示経路が別のため）。

`motor_check` は HTTP POST 経路が `_handle_command` を通らないため、`_start_motor_check` 側にも
同じフェーズ判定を置いている（片方だけでは穴が空く）。

### 試合開始の挙動（モード別）

- **半自動**: `match_start` はフェーズを `match` にするだけ。各操縦者が自分のタブで `sequence_start` を押す
  （ハンドごとに開始タイミングが異なるため）
- **全自動**: 操縦者タブが無いので `match_start` が両ロボットの `request_start()` を兼ねる

### 全自動モードの実現方式

シーケンス定義は半自動と共用し、`Sequence.set_auto_advance(True)` で `require_trigger` のステップを
待たずに通過させる。二重メンテを避け、半自動と全自動で挙動が乖離しないようにするため。

人間の目視確認が必須な危険動作には `@step("...", require_trigger=True, auto_stop=True)` を付ける。
`auto_stop=True` のステップは全自動でも必ずトリガー待ちで停止する。

### コート対応

`Sequence.court` で参照できる。実際の左右反転は各 `robots/*.py` の責務。
座標定数が未確定のため現状は土台のみで、確定後に反転テーブル／`court_offsets` を導入する。

### WebSocket プロトコル拡張

Server → Client（**WS 接続直後に 1 回 + 変化時**）。接続直後に送らないと、リロードした操縦者が
現在のモード・フェーズを知れない:

```jsonc
{
  "type": "match_state",
  "mode": "semi_auto",
  "court": "red",
  "phase": "setup",
  "required_roles": ["main_hand", "sub_hand"],
  "can_start_match": false,
  "checklists": {
    "monitor":   { "items": [{ "id": "power", "label": "電源投入確認", "checked": false }], "completed": false },
    "main_hand": { "items": [/* ... */], "completed": false },
    "sub_hand":  { "items": [/* ... */], "completed": false }
  }
}
{ "type": "command_rejected", "command": "sequence_start", "reason": "試合中のみシーケンスを開始できます" }
```

Client → Server:

```jsonc
{ "type": "set_mode", "mode": "full_auto" }
{ "type": "set_court", "court": "blue" }
{ "type": "checklist_set", "role": "main_hand", "item_id": "home_position", "checked": true }
{ "type": "checklist_reset", "role": "main_hand" }   // role 省略で全ロール
{ "type": "match_start" }
{ "type": "match_finish" }
{ "type": "match_reset" }
```

### チェックリスト設定（`config/checklist.yaml`）

```yaml
checklists:
  monitor:
    - { id: power, label: 電源投入・バッテリ電圧確認 }
  main_hand:
    - { id: home_position, label: メインハンド初期位置確認 }
  sub_hand:
    - { id: home_position, label: サブハンド初期位置確認 }
```

`id` はロール内で一意。`id` / `label` を欠くエントリは無視して起動する
（yaml の記述ミスで起動が落ちるより、UI 上で項目欠落に気付ける方が競技当日の運用に適する）。
`--checklist <path>` でパスを差し替え可能。ファイルが無ければ項目ゼロで起動する。

---

## Web UI ページ構成

タブ構成（`web/src/App.tsx`）。操縦者 2 名はそれぞれ Main Hand / Sub Hand タブを開く。

| タブ | ページ | 内容 |
|---|---|---|
| Monitor | `pages/Dashboard.tsx` | 試合制御 (`MatchControl`)、全自動時の指差喚呼、半自動時は 2 名の進捗監視、両ロボット概要 |
| Main Hand | `pages/RobotControl.tsx` | 半自動セッティング中は指差喚呼、試合中はシーケンス操作 |
| Sub Hand | `pages/RobotControl.tsx` | 同上 |
| PID Tuning | `pages/MotorTuning.tsx` | モータ個別調整 |

- ステータスバーに `PhaseBanner`（MODE / COURT / PHASE）を常時表示。誤ったコート設定のまま試合に入る事故を防ぐ
- `command_rejected` は画面下部にトースト表示（4 秒で自動消去）
- 試合中以外は START / NEXT / ステップジャンプを UI 上でも無効化する（サーバー側ゲートとの二重防御）

---

## config.yaml 構造

```yaml
robot_name: main_hand

can_buses:
  m3508_bus: can0
  edulite_bus: can1
  generic_bus: can2

motors:
  lift_motor:
    driver: m3508
    bus: m3508_bus
    can_id: 1
  arm_joint:
    driver: edulite05
    bus: edulite_bus
    can_id: 1
    mode: position
  gripper:
    driver: generic
    bus: generic_bus
    can_id: 0x01
    control_type: position
```

---

## シーケンス記述例

```python
from lib.sequence.engine import Sequence, step

class PickAndPlace(Sequence):
    @step("初期位置へ移動")
    async def move_to_home(self):
        await self.motors.m3508_1.set_position(0)
        await self.motors.edulite_1.set_position(0)
        await self.wait_all_reached()

    @step("アーム展開", require_trigger=True)
    async def extend_arm(self):
        await self.motors.m3508_1.set_position(1500)
        await self.wait_all_reached()

    @step("ハンド閉じる")
    async def close_hand(self):
        await self.motors.servo_1.set_angle(90)
        await asyncio.sleep(0.5)
```

---

## 実装フェーズ

### Phase 1: 基盤（CAN 通信レイヤー）— TDD

| # | ファイル | 内容 |
|---|---|---|
| 1-1 | `pyproject.toml` | プロジェクト設定（uv 管理）。依存: python-can, aiohttp, pyyaml / dev: pytest, pytest-asyncio, ruff |
| 1-2 | `lib/drivers/base.py` | MotorDriver 基底クラス（set_position, set_velocity, get_state 等のインターフェース） |
| 1-3 | `tests/drivers/test_m3508.py` | **テスト先行**: M3508 エンコード/デコードのテストを書く |
| 1-4 | `lib/drivers/m3508.py` | C620 プロトコル実装（テストを GREEN にする） |
| 1-5 | `tests/drivers/test_edulite05.py` | **テスト先行**: EDULITE 05 のテストを書く |
| 1-6 | `lib/drivers/edulite05.py` | RobStride プロトコル実装（テストを GREEN にする） |
| 1-7 | `tests/drivers/test_generic.py` | **テスト先行**: 自作モタドラプロトコルのテストを書く |
| 1-8 | `lib/drivers/generic.py` | 自作モタドラプロトコル実装（テストを GREEN にする） |
| 1-9 | `lib/can_manager.py` | python-can の asyncio ラッパー。バス名 → Bus オブジェクト管理、送受信キュー |
| 1-10 | `tests/test_can_manager.py` | vcan を使った CAN 送受信の統合テスト |

### Phase 2: シーケンスエンジン — TDD

| # | ファイル | 内容 |
|---|---|---|
| 2-1 | `tests/test_sequence_engine.py` | **テスト先行**: ステップ遷移、trigger 待ち、エラー処理のテスト |
| 2-2 | `lib/sequence/engine.py` | シーケンスエンジン実装（テストを GREEN にする） |
| 2-3 | `config/main_hand.yaml` | モータ定義（名前、ドライバ種別、CAN バス、CAN ID、制御モード） |
| 2-4 | `config/sub_hand.yaml` | 同上 |

### Phase 3: サーバー + WebSocket

| # | ファイル | 内容 |
|---|---|---|
| 3-1 | `tests/test_ws_protocol.py` | **テスト先行**: WebSocket JSON プロトコルのパース/生成テスト |
| 3-2 | `lib/server.py` | aiohttp で HTTP（静的ファイル配信）+ WebSocket 統合。JSON プロトコルでの状態配信・コマンド受信 |
| 3-3 | `main.py` | config 読み込み → CAN 初期化 → シーケンス登録 → サーバー起動。asyncio.gather で全部回す |

### Phase 4: Web UI

#### ツール構成（2026-04 確定）

| 項目 | 採用 | 補足 |
|---|---|---|
| パッケージマネージャ | **pnpm@10**（`packageManager` フィールドで固定）| `web/.npmrc` に `auto-install-peers=true` |
| Linter | **oxlint** | `web/.oxlintrc.json`、react/typescript/unicorn/import/jsx-a11y プラグイン |
| Formatter | **oxfmt** (Beta) | `web/.oxfmtrc.json`、Tailwind ソート + import ソート組み込み |
| ESLint / Prettier | 不採用（削除済み）| oxlint + oxfmt に集約 |
| フォント | `@fontsource/inter` + `@fontsource-variable/noto-sans-jp` + `@fontsource-variable/jetbrains-mono`（自己ホスト） | Tailwind `@theme` の `--font-sans` で英→Inter、日本語→Noto Sans JP のグリフ単位フォールバック |
| UI ライブラリ | **TuiCss（CSS のみ）+ 自前 `tui/` プリミティブ** | 旧 HeroUI v3 から全面移行（後述「TUI リデザイン」参照）。`tuicss.js` は React と DOM 制御が競合するため不使用、モーダル/タブは React state で制御 |
| アイコン | **不採用（Unicode/ASCII 記号化）** | 旧 `lucide-react` を撤去。◆◇▲▼█░ 等の記号で代替し依存ゼロ化 |
| テーマ | **TUI 配色（IBM CGA 風・濃青背景＋白文字＋等幅）** | `web/src/index.css` の `:root` で `--tui-*` パレットと `--font-tui` を定義。html/body のベース色だけ `@theme` の `--color-bg`/`--color-text` を残す |
| 実行環境 | **node/pnpm は mise 経由** | `mise exec -- pnpm <cmd>`（素のシェルには node が無い） |
| scripts | `dev` / `build` / `preview` / `lint` / `lint:fix` / `format` / `format:check` / `check` | `check` = `lint && format:check && tsc -b --noEmit` |

#### TUI リデザイン（2026-06 全面移行）

操縦 UI を HeroUI v3（Tailwind v4 + React Aria）から **古典 TUI 風（TuiCss CSS のみ + 自前プリミティブ）** に全面置換した。

- **依存削除**: `@heroui/react` / `@heroui/styles` / `lucide-react` を `pnpm remove`。`index.css` の `@import "@heroui/styles";` も除去。残依存は `tuicss`（CSS）と `@fontsource*`・Tailwind のみ。
- **自前プリミティブ**: `web/src/components/tui/` に `TuiWindow` / `TuiPanel` / `TuiFieldset` / `TuiButton` / `TuiNav` / `TuiStatusbar` / `TuiTable` / `TuiProgress` / `TuiModal` / `TuiClock` を実装（`index.ts` で再エクスポート、`types.ts` に共通型）。
- **tuicss.js 不使用**: モーダルの開閉・タブの active 切替は React state で制御し、`.tui-*` 補助クラス（`tui-shell` / `tui-fill` / `tui-col` / `tui-row` / `tui-scroll` 等）でレイアウトを補う。
- **アイコン撤去**: lucide を全廃し Unicode/ASCII 記号（◆◇▲▼█░ 等）で代替。
- **レイアウト**: ルート `.tui-shell`（100vw × 100svh, `overflow:hidden`）配下に AppHeader（固定）＋ flex-1 の main ＋ Statusbar（固定）。全体スクロールは禁止し、スクロールは `.tui-scroll` 領域内のみ（`min-h-0` チェーンで担保）。
- **不変点**: WebSocket 送受信ロジック（`useRobotSocket` / `RobotContext` / `useMotorCheck`）とメッセージ型は未変更。今回の変更は UI 層のみ。

#### ファイル一覧

| # | ファイル | 内容 |
|---|---|---|
| 4-1 | `web/` scaffold | Vite + React + React Router + TypeScript 初期セットアップ |
| 4-2 | `useRobotSocket.ts` | WebSocket 接続管理、自動再接続、状態パース、`e_stop_state` 専用イベント受信 |
| 4-3 | `Dashboard.tsx` | 両ロボットの状態概要を Card 化して表示、操縦画面へのリンク |
| 4-4 | `RobotControl.tsx` | 操作画面: SequenceProgress + 大型 TriggerButton + MotorSummary（折りたたみ）|
| 4-5 | `MotorTuning.tsx` | モータごとの状態 + PID パラメータ調整（Slider + 送信ボタン） |
| 4-6 | `EStopButton.tsx` | ヘッダー右に常設。記号（◆）+ 黄黒ストライプ装飾（TUI リデザインで lucide 撤去） |
| 4-7 | `EStopOverlay.tsx` | 全画面赤フラッシュ + パルスリング + 進捗リング SVG。時計回り 90° ツイストで解除 |
| 4-8 | `AppHeader.tsx` | 共通ヘッダー（記号化）+ TuiNav タブ + 全画面切替（TUI リデザインで lucide/Drawer 撤去） |
| 4-9 | `router.tsx` | レイアウトルートで AppHeader と EStopOverlay を一元化、各ページから重複排除 |
| 4-10 | `components/Icon.tsx`, `StatusDot.tsx`, `StatPill.tsx`, `ConnectionStatus.tsx`, `SequenceProgress.tsx`, `MotorStatus.tsx`, `MotorSummary.tsx`, `TriggerButton.tsx` | デザイントークンに準拠した共通 UI 部品 |
| 4-11 | `index.css` / `index.html` | TUI パレット（`:root` の `--tui-*`）+ `.tui-*` 補助クラス + 各種 keyframe（e-stop-flash / trigger-glow / connection-dot）。`@theme` は html/body ベース色（`--color-bg`/`--color-text`）とフォント変数のみ保持し、未使用の HeroUI 設計トークン（surface/accent/shadow/radius 等）は削除済み |

### Phase 5: ロボット固有シーケンス

| # | ファイル | 内容 |
|---|---|---|
| 5-1 | `robots/main_hand.py` | メインハンドのシーケンスクラス（担当者が記述） |
| 5-2 | `robots/sub_hand.py` | サブハンドのシーケンスクラス（担当者が記述） |

### Phase 6: CAN Bus ヘルスチェック — TDD

運用中に検出したい異常は H1 バス断線/バスオフ、H2 モータ無応答、H3 バス輻輳/エラー多発、H4 モータ自身の異常（過熱・過電流）の 4 種類。受動監視（受信タイムスタンプ + 送信失敗例外）を主体とし、能動 ping は明示要求時のみとする。状態は WS 配信と `GET /health` の両方で公開する。

#### データ構造

```python
# lib/health.py
class BusHealth(Enum): OK / DEGRADED / DOWN
class MotorHealth(Enum): OK / STALE / WARNING / FAULT

@dataclass BusHealthInfo:
    name, channel, state, last_tx_at, last_rx_at,
    tx_error_count, rx_error_count, bus_off

@dataclass MotorHealthInfo:
    name, bus, state, last_feedback_at, feedback_age_ms,
    temperature, detail

@dataclass HealthSnapshot:
    timestamp, overall, buses, motors
```

#### WebSocket プロトコル拡張

Server → Client `state` メッセージに `health` フィールドを同梱:

```jsonc
{
  "type": "state",
  "robot": "main_hand",
  ...,
  "health": {
    "overall": "ok",
    "buses": [{ "name": "m3508_bus", "channel": "can0", "state": "ok",
                "last_rx_at": 1714377600.12, "tx_error_count": 0, "bus_off": false }],
    "motors": [{ "name": "lift_motor", "bus": "m3508_bus", "state": "ok",
                 "feedback_age_ms": 23.4, "temperature": 35.0 }]
  }
}
```

状態遷移の瞬間に push する専用イベント:
```jsonc
{ "type": "health_change", "level": "critical",
  "target": "bus:m3508_bus", "from": "ok", "to": "down",
  "message": "can0 bus_off detected" }
```

Client → Server の即時要求:
```jsonc
{ "type": "health_check" }
```

#### HTTP エンドポイント

`GET /health` → `HealthSnapshot` を JSON で返す。`overall` に応じて HTTP ステータス 200 (OK) / 503 (DEGRADED|DOWN) を返却。CI・監視ツール・`curl` 動作確認用。

#### config（既定値）

```yaml
health:
  feedback_timeout_ms: 500     # この時間フィードバックなければ STALE
  bus_check_interval_ms: 1000  # bus.state ポーリング周期
  temp_warning_c: 65
  temp_critical_c: 80
  tx_error_threshold: 96       # CAN 標準: error_passive 境界
```

#### 実装タスク

| # | ファイル | 内容 |
|---|---|---|
| 6-1 | `tests/test_health.py` | **テスト先行**: しきい値判定、状態遷移（ヒステリシス含む）、JSON シリアライズ |
| 6-2 | `lib/health.py` | `BusHealth` / `MotorHealth` 列挙、`*HealthInfo` / `HealthSnapshot` dataclass、JSON 化 |
| 6-3 | `lib/drivers/base.py` (修正) | `MotorDriver` に `has_thermal_warning()` / `has_overcurrent_warning()` / `is_fault()` のデフォルト実装を追加 |
| 6-4 | `lib/drivers/m3508.py` (修正) | C620 フィードバックの温度・電流からフラグ判定 |
| 6-5 | `lib/drivers/edulite05.py` (修正) | RobStride のステータス領域を解釈 |
| 6-6 | `lib/drivers/generic.py` (修正) | フィードバック Byte7 の bit0=到達 / bit1=過電流 / bit2=過熱 を解釈 |
| 6-7 | `tests/test_can_manager_health.py` | **テスト先行**: vcan で 受信タイムアウト → STALE、送信失敗 → DOWN 遷移、`bus.state` 反映 |
| 6-8 | `lib/can_manager.py` (修正) | 送受信時刻記録、`bus.state` ポーリング、`health()` メソッド、`_health_check_loop` 追加 |
| 6-9 | `tests/test_server_health.py` | **テスト先行**: WS state に `health` 同梱、`GET /health` の 200/503、`health_change` push |
| 6-10 | `lib/server.py` (修正) | `_build_state_message` で health 同梱、`/health` ルート追加、状態遷移検出で `health_change` push |
| 6-11 | `config/*.yaml` (修正) | `health:` セクション追加（既定値は上記） |
| 6-12 | `web/src/components/HealthIndicator.tsx` | バス/モータごとに信号灯（緑黄赤）+ 詳細ツールチップ |
| 6-13 | `web/src/pages/Dashboard.tsx` (修正) | ヘッダ近傍に overall 表示、警告時はトースト通知 |
| 6-14 | `web/src/hooks/useRobotSocket.ts` (修正) | `health` パース、`health_change` ハンドリング |

#### 段階的実装計画

| 段階 | 成果物 | 動作確認 |
|---|---|---|
| ① データ型 | 6-1, 6-2 | `pytest tests/test_health.py` |
| ② ドライバ拡張 | 6-3〜6-6 | 既存ドライバテストに warning/fault 判定を追加 |
| ③ CANManager 拡張 | 6-7, 6-8 | vcan で送信止めて 600ms 後 STALE、shutdown で DOWN 遷移を確認 |
| ④ サーバー統合 | 6-9, 6-10 | `aiohttp.test_utils` で `GET /health` 200/503、WS ペイロード検証 |
| ⑤ config 反映 | 6-11 | dry-run 起動でしきい値読み込み確認 |
| ⑥ Web UI | 6-12〜6-14 | `npm run dev` で表示。config しきい値を短くして遷移を目視 |

#### リスクと回避策

| リスク | 回避策 |
|---|---|
| ヘルスチェックループが CAN 受信を阻害 | 受動監視主体・能動 ping は明示要求時のみ |
| しきい値が厳しすぎて誤警報（チャタリング） | config で上書き可能。STALE→OK 復帰には連続 N フレーム受信を要求 |
| 送信エラーで `_receive_loop` が落ちる | `send_to_bus` の例外を握って health に反映、ループは継続 |
| bus_off からの自動復帰 | `bus.recover()` を試行回数制限付きで呼び、ログに残す |

#### アクチュエータ動作確認シーケンス

受動監視（H1〜H4）を補完する **能動テスト**。Web UI の「動作確認」ボタンから起動し、各モータを 1 つずつ微小駆動して指令への応答を視覚的に確認する。

##### コンセプト

- 受動監視 = 「いま壊れていないか？」、能動テスト = 「いま指示を出したら正しく動くか？」を別物として扱う
- 通常シーケンス（main_hand / sub_hand）と **同じエンジンを使い回さない**。`MotorCheckRunner` が独立して 1 モータずつ駆動・元の状態に戻す
- 緊急停止中・通常シーケンス実行中・バス DOWN 時はボタンを無効化（誤操作防止）

##### モータごとの判定ロジック

| ドライバ | 投入指令 | 判定基準 |
|---|---|---|
| **M3508** (電流制御) | 目標電流 ±500 mA を 1s 印加 | 1s 以内にフィードバック受信 + `velocity` の符号が指令電流と一致 |
| **EDULITE 05** (位置制御) | 現在位置 ±5° を 1s 指令 | 1s 以内にフィードバック受信 + `position` が目標±許容に到達 |
| **Generic** (位置/速度制御) | `control_type` に応じた微小目標 | フィードバック受信 + `reached` フラグ立ち上がり、過電流/過熱フラグなし |

判定ロジックを呼び出し側に漏らさないよう、`MotorDriver` 基底クラスに `check_command(*, magnitude)` / `evaluate_check_result(state, target)` を定義し各ドライバが自身の動作確認パラメータを保持。

##### データ構造（`lib/health.py` に追加）

```python
class MotorCheckResult(Enum):
    PENDING / RUNNING / PASSED / FAILED / TIMEOUT / SKIPPED

@dataclass MotorCheckRecord:
    motor, bus, started_at, finished_at, result,
    expected, observed, detail

@dataclass CheckRunSnapshot:
    robot, started_at, finished_at,
    overall,  # "running" | "ok" | "partial" | "failed"
    records: list[MotorCheckRecord]
```

##### WebSocket / HTTP プロトコル

Client → Server:
```jsonc
{ "type": "motor_check_start", "robot": "main_hand" }
{ "type": "motor_check_abort", "robot": "main_hand" }
```

Server → Client（実行中ストリーム）:
```jsonc
{ "type": "motor_check_progress", "robot": "main_hand",
  "current": "arm_joint", "index": 1, "total": 4 }

{ "type": "motor_check_record", "robot": "main_hand",
  "record": { "motor": "lift_motor", "result": "passed",
              "expected": 500, "observed": 487.2, "detail": null } }

{ "type": "motor_check_done", "robot": "main_hand",
  "snapshot": { ...CheckRunSnapshot... } }
```

HTTP:
- `POST /robots/{robot}/motor_check` → 起動。即時 `{ "started": true }` を返し、結果は WS で配信
- `GET /robots/{robot}/motor_check/last` → 直近結果のスナップショット

##### 実行フロー

```
RobotServer.handle("motor_check_start")
  └─ MotorCheckRunner(robot, can_manager, motors).run()
       1) 緊急停止 / 通常シーケンス実行中なら拒否
       2) ロックを取り、CheckRunSnapshot を初期化
       3) for motor in motors:
            - record.result = RUNNING / WS push (motor_check_progress)
            - msg = motor.check_command()
            - last_rx_at の現在値を記録
            - send + 観測待ち（タイムアウト T 秒）
            - motor.evaluate_check_result(state, target) → PASSED/FAILED
            - 元の位置 / 0 電流に戻す指令を必ず送る
            - WS push (motor_check_record)
       4) overall = all/some/none passed
       5) WS push (motor_check_done) / lock release
```

##### 安全策

- 動作確認シーケンス開始前に **確認ダイアログ必須**（「全モータを順番に微小駆動します。周囲の安全を確認してください」）
- 各モータの指令量は **物理的に安全な微小量に固定**（config で上書き可能）
- 動作確認実行中も **緊急停止コマンドは即時優先**（既存 e_stop 経路）
- M3508 の電流指令はリリース時に必ず 0 を再送（駆動状態を残さない）

##### config（既定値）

```yaml
motor_check:
  per_motor_timeout_ms: 1500     # 1 モータあたりのタイムアウト
  default_magnitude:
    m3508: 500                   # mA
    edulite05: 5.0               # deg
    generic: 0.1                 # 0.1 rev / 10% duty 等（control_type 依存）
  tolerance:
    edulite05_deg: 1.0
```

config の `motors` 内で個別上書き:
```yaml
motors:
  lift_motor:
    driver: m3508
    bus: m3508_bus
    can_id: 1
    motor_check:
      magnitude: 800             # この個体のみ 800mA で確認
      timeout_ms: 2000
```

##### 実装タスク

| # | ファイル | 内容 |
|---|---|---|
| 6-15 | `tests/test_motor_check.py` | **テスト先行**: モック CAN で PASSED/FAILED/TIMEOUT、abort、競合（通常シーケンス中 / 緊急停止中の拒否）|
| 6-16 | `lib/drivers/base.py` (修正) | `check_command(*, magnitude)` / `evaluate_check_result(state, target)` 抽象メソッド + デフォルト実装 |
| 6-17 | `lib/drivers/m3508.py` (修正) | 電流指令版の check 実装 |
| 6-18 | `lib/drivers/edulite05.py` (修正) | 位置指令版の check 実装 |
| 6-19 | `lib/drivers/generic.py` (修正) | `control_type` に応じた check 実装 |
| 6-20 | `lib/motor_check.py` (新規) | `MotorCheckRunner`, `CheckRunSnapshot`, `MotorCheckRecord` |
| 6-21 | `tests/test_server_motor_check.py` | **テスト先行**: WS の `motor_check_start` / `_progress` / `_record` / `_done` の流れ、緊急停止中・シーケンス中の拒否 |
| 6-22 | `lib/server.py` (修正) | コマンドハンドラ追加（`motor_check_start` / `_abort`）+ HTTP ルート + WS イベント発火 |
| 6-23 | `config/*.yaml` (修正) | `motor_check:` セクション + モータ単位の上書き |
| 6-24 | `web/src/hooks/useMotorCheck.ts` | WS イベント集約 hook |
| 6-25 | `web/src/components/MotorCheckButton.tsx` | ヘッダボタン + 確認ダイアログ。緊急停止中 / シーケンス中 / バス DOWN で無効化 |
| 6-26 | `web/src/components/MotorCheckPanel.tsx` | 実行中の進捗 + モータごとの ✓×、終了後はサマリ + リトライ |
| 6-27 | `web/src/pages/Dashboard.tsx` (修正) | パネル組み込み |

##### 段階追加

| 段階 | 成果物 | 動作確認 |
|---|---|---|
| ⑦ ドライバ check API | 6-15〜6-19 | 各ドライバ単体テスト（PASSED/FAILED/TIMEOUT） |
| ⑧ MotorCheckRunner | 6-20 | モック CAN で全シナリオ再現 + abort 動作確認 |
| ⑨ サーバー統合 | 6-21, 6-22 | WS で `_start` → 進捗 → 完了の一連を確認、競合拒否 |
| ⑩ config 反映 | 6-23 | dry-run でモータ別パラメータが効くか |
| ⑪ Web UI | 6-24〜6-27 | dry-run + Web UI から実押下・結果表示・無効化ロジックの目視確認 |

### Phase 7: 試合運用フロー（モード / コート / フェーズ / 指差喚呼）— TDD

詳細な仕様は「試合運用フロー」セクションを参照。

| # | ファイル | 内容 |
|---|---|---|
| 7-1 | `lib/match_state.py` | Mode / Court / Phase / ChecklistItem / MatchState / `load_checklist_definitions` |
| 7-2 | `tests/test_match_state.py` | 遷移規則・完了判定・モード切替時リセット・コマンドゲートの単体テスト |
| 7-3 | `lib/sequence/engine.py` | `court` / `auto_advance` / `@step(auto_stop=True)` を追加 |
| 7-4 | `tests/test_sequence_court_auto.py` | コート伝播と全自動通過／`auto_stop` 停止の検証 |
| 7-5 | `lib/server.py` | `MatchState` 保持、フェーズゲート、`match_state` 配信、接続直後スナップショット、自動開始の廃止 |
| 7-6 | `tests/test_server_match.py` | WS 経由の全フローと HTTP `motor_check` ゲートの検証 |
| 7-7 | `config/checklist.yaml`, `main.py` | チェックリスト定義の読み込みと `--checklist` オプション |
| 7-8 | `web/src/hooks/useRobotSocket.ts` | `match_state` / `command_rejected` の受信 |
| 7-9 | `web/src/components/{Checklist,MatchControl,PhaseBanner}.tsx` | 新規 UI コンポーネント |
| 7-10 | `web/src/pages/{Dashboard,RobotControl}.tsx` | Monitor の試合制御、操縦者タブのフェーズ別表示 |

#### 設計上の判断

- **状態の正はサーバー**: 操縦者 2 名 + Monitor が別ブラウザで接続するため、
  クライアントローカルでは「2 人とも完了」を判定できない
- **`mode` と `phase` は直交**: 混在させると状態が破綻するので 2 軸に分離した
- **ゲートは二重**: UI の無効化だけでは WS 直叩き・リロード直後を防げないので
  サーバー側 (`deny_reason`) を単一の判定点とする
- **全自動は共用シーケンス + フラグ**: 専用シーケンスを別定義すると二重メンテになり、
  半自動と全自動で挙動が乖離する

---

## RobStride EDULITE 05 プロトコル概要

調査結果のサマリー。RobStride シリーズは全モデル共通プロトコル。

- **CAN 2.0B Extended Frame（29bit ID）、1Mbps**
- 29bit ID 構造: `[通信タイプ 5bit][データエリア2 16bit][宛先ID 8bit]`
- デフォルト モータ ID: `0x7F`
- 制御モード: MIT(0) / 位置(1) / 速度(2) / 電流(3)
- フィードバック: 角度・角速度・トルク・温度（各 16bit → 物理量に線形マッピング）

### 主要リソース

- 公式 GitHub: https://github.com/RobStride
- EDULITE A3 (Python SDK + ROS2): https://github.com/RobStride/EDULITE_A3
- STM32 サンプル: https://github.com/RobStride/SampleProgram
- Rust crate: https://docs.rs/robstride/latest/robstride/
- Seeed Studio Wiki: https://wiki.seeedstudio.com/robstride_control/
