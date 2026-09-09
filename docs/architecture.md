# アーキテクチャ — 何がどう組まれているか

キャチロボバトルコンテスト 2026 出場ロボットの中央制御プログラムの構造を書く。
層構成・データフロー・契約・一覧表がここにあり、**「なぜそう組んだか」「崩すと何が
起きるか」は [`invariants.md`](invariants.md) が持つ**（同じ話を両方には置かない）。
**点検・ヘルス・動作確認の「今どうなっているか」は
[`checks_and_health.md`](checks_and_health.md)**、自作モータドライバの CAN フレーム定義は
[`motor_driver_can_protocol.md`](motor_driver_can_protocol.md)、実装の経緯は
[`history/`](history/) が正である。

- [1. 全体像](#1-全体像)
- [2. ハードウェア構成](#2-ハードウェア構成)
- [3. ディレクトリとモジュール構成](#3-ディレクトリとモジュール構成)
- [4. 制御レイヤ](#4-制御レイヤ)
- [5. シーケンス](#5-シーケンス)
- [6. 設定ファイルの構成](#6-設定ファイルの構成)
- [7. WebSocket プロトコル](#7-websocket-プロトコル)
- [8. 試合運用フロー](#8-試合運用フロー)
- [9. Web UI の構成](#9-web-ui-の構成)
- [10. サービス運用（systemd）](#10-サービス運用systemd)
- [11. テスト戦略](#11-テスト戦略)
- [12. 未解決の課題](#12-未解決の課題)
- [13. 参考リンク](#13-参考リンク)

---

## 1. 全体像

固定型ロボット 2 台（メインハンド / サブハンド）を**同一 PC 上の 1 プロセス**で制御する。
操縦者 2 名 + Monitor の 3 画面がブラウザから WebSocket で繋がり、半自動シーケンスを
進める。

### プロセス構成

制御プログラムと Web Controller は**同一プロセス**である。`lib/server.py` の
`create_app()` が aiohttp で `web/dist/` を SPA 配信し、同じポート（既定 8080）で
WebSocket (`/ws`) を受ける。

```
        ブラウザ ×3（操縦者 2 + Monitor。別 PC でもよい）
              │  WebSocket (JSON) + HTTP(SPA)
┌─────────────▼────────────────────────────────────────────────┐
│  main.py（asyncio 単一プロセス / localhost:8080）              │
│   aiohttp Server ── RobotServer ── MatchState                 │
│    (server.py)      (server.py)    (match_state.py)           │
│         │  WsHub          │  handle_command                   │
│         │ (ws_hub.py)     ├── MotorCheckController            │
│         │                 ├── ManualController (manual.py)    │
│         │                 └── Sequence ×3 (sequence/engine)   │
│   周期タスク（control/periodic.py 継承）  ┐                     │
│     M3508PositionLoop 200Hz / SyncMonitor 50Hz /              │
│     LimitMonitor 50Hz /                                       │
│     GenericTargetRefresher・QueryDrivenTargetRefresher 20Hz   │
│                    CANManager（can_manager.py）                │
│         ドライバ: m3508 / edulite05 / dm3520 / generic         │
│         受信ループ ×バス本数                                    │
└──────────────────────────┬───────────────────────────────────┘
                           │ SocketCAN
   can_m3508 / can_edulite / can_generic / can_dm3520
    M3508×2    EDULITE 05×3  自作モタドラ    DM3520×2
                          （DC/サーボ/電磁弁）
```

### 技術スタック

| 層 | 採用 |
|---|---|
| バックエンド | Python 3.12+ / asyncio（単一プロセス）。パッケージ管理は **uv** |
| CAN 通信 | python-can + SocketCAN |
| HTTP + WS サーバー | aiohttp（静的配信と WebSocket を 1 プロセスに統合） |
| Web UI | Vite + React + TypeScript + Tailwind v4 / daisyUI 5 / React Router |
| ファームウェア | PlatformIO（DC / サーボ）+ CubeMX + CMake（電磁弁のみ） |
| リント・整形 | ruff（Python）/ oxlint + prettier（web） |
| テスト | pytest + pytest-asyncio / vitest / PlatformIO native (Unity) |

ROS 2 は不採用（固定型 + 一本道シーケンスでは DDS の利点が薄く、WebSocket 統合に
不要な複雑性が乗る）。

### データフロー

**指令**（操縦者 → 機体）:

```
ブラウザのボタン
  → WS メッセージ（lib/commands.py の CommandSpec に載る語彙のみ）
  → RobotServer.handle_command（3 段ゲート: 開発用 → フェーズ → 緊急停止）
  → _cmd_* ハンドラ
  → Sequence.trigger / ManualController / MotorCheckController
  → AxisHandle.set_target_value（軸単位。モータ単位の口は無い）
  → MotorHandle.set_target → ドライバ .encode_target → CANManager.send_to_bus
      ただし M3508 だけは target_sink で M3508PositionLoop へ渡り、
      200Hz のループが 0x200 フレームを組み立てて送る
```

**テレメトリ**（機体 → 画面）:

```
CAN フレーム
  → CANManager._receive_loop（バス 1 本に 1 つ）
  → _dispatch_frame（モータ 1 台単位で matches_feedback → update_state）
  → ドライバの MotorState / MotorHealth
  → RobotServer._build_state_message（_measured_only() で測れない項目を null へ）
  → WsHub.fanout（唯一の配信経路）
  → protocol.ts の parseServerMessage（受信条件の単一情報源）
  → robotReducer.ts（純関数）→ RobotContext → 各画面
```

**配信の頻度**: `state` は定期配信（既定 50ms 周期）。`match_state` / `e_stop_state` /
`motor_check_state` / `homing_state` / `switch_measure_state` / `health_change` は変化時に push。`server_info` は
接続直後の 1 回だけ。

### 起動と後始末の段（`main.py`）

`main()` は **「読む → bind の可否を見る → 配線する → 起動する → 畳む」** の 4 段
（+ 事前確認 1 段）で構成する。ロボット 1 台ぶんの配線は `_wire_one_robot` が持つ。

| 段 | 関数 | 内容 |
|---|---|---|
| 読む | `_load_all_configs` | `lib/config_schema.py` の 4 ローダを呼ぶ。誤記は `SystemExit` の 1 行 |
| bind | `_ensure_port_available(host, port)` | 配線の**手前**で行う（会場での二重起動を CAN を開く前に落とす） |
| 配線 | `_wire_one_robot` ×ロボット数 | 下表 |
| 起動 | `_start_all` | `CANManager.run()` → 位置制御ループ → 同期監視 → 可動端監視 → 目標値再送 → サーバー |
| 畳む | `_shutdown_all` | 位置制御ループ → 目標値再送 → 可動端監視 → 同期監視 → CAN → サーバーの順（順序は不変条件） |

`_wire_one_robot` が呼ぶヘルパ:

| 関数 | 役割 |
|---|---|
| `_load_sequence(robot_name)` | `sequences.<robot_name>` を動的 import し、**そのモジュール自身が定義した** `Sequence` サブクラスを 1 つだけ拾う（2 つ以上なら起動拒否） |
| `_DRIVER_MAP` | 「ドライバ種別 → 生成関数」のファクトリ表。4 種すべてがここを通る |
| `_load_pid_config` / `_build_position_pid` / `_build_position_loops` | `motors[name].pid` を読んで `_DEFAULT_PID` で補完し出力レンジを絞り、M3508 が居る**バスごとに 1 つ** `M3508PositionLoop` を生成 |
| `_build_sync_groups` / `_attach_sync_groups` | `PositionTable.paired_axes()` → `SyncGroup`。全メンバが同一ループに載るものだけループへ登録 |
| `_attach_motion_profiles` | `axes.<軸>.motion` を持つ軸へ台形プロファイルを後付けする |
| `_wire_robot_motors` | `build_motor_group()` → `Sequence.bind_motors()` |
| `_build_target_refresher(s)` | generic 用と問い合わせ駆動（EDULITE 05 / DM3520）用の 20Hz 再送 |
| `_make_origin_resolver` | 零点確定の手段（PC 側位置制御ループ / ドライバの `SET_ZERO`）を解決 |
| `_build_limit_monitors` | `axes.<軸>.guard.limits` を書いた軸の移動中インターロック（`LimitMonitor`）。対象が 1 本も無ければ回さない |
| `_make_sensor_reader` / `_make_sensor_contact_reader` | 可動端インターロックと零点確定が共有するセンサの読み口。前者は三値の現在値（`None` = 読めていない）、後者は接触（OFF→ON）の累計（`None` = カウンタを提供しないドライバ） |
| `_make_limit_interventions` | `LimitMonitor` が止めた回数を `Sequence.bind_limit_interventions` へ渡す（保護に曲げられた `move_to` を失敗させる） |
| `_build_manual_controller` | シーケンスと**同じ** `MotorGroup` を共有する `ManualController` |
| `_wire_motor_check_sequence` | 両ハンドの `MotorHandle` と `PositionTable.merged` を統合動作確認へ渡す |
| `_read_operstate` | `/sys/class/net/<ch>/operstate` を読み、down なら起動ログへ ERROR 1 行（起動は止めない） |

生成した部品は `server.add_robot(robot_name, seq, can_manager, position_loops=…,
sync_monitors=…, limit_monitors=…, target_refreshers=…)` でサーバーへも渡す（サーバーはこれを①動作確認との
排他 ②緊急停止解除でのラッチ解除 ③緊急停止時の保持目標の破棄 ④安全ループの生死配信 に使う）。
**開くバスはそのロボットが実際に使うものだけ**（`_robot_bus_names`。メインハンドは
`can_dm3520` を、サブハンドは `can_m3508` を開かない）。シグナルの扱いは
`_install_stop_signal_handler()` が SIGTERM を main タスクの `cancel()` へ変換して
`except asyncio.CancelledError` → `finally` の後始末経路へ合流させ、各段を `_shutdown_step()`
で包んで 1 つが失敗しても残りを続け、最終行に `後始末完了` を残す。

### 起動オプション

`--dry-run`（CAN 無し）/ `--dev-tools`（開発用コマンドの解禁）/ `--log-level` /
`--system` `--config` `--checklist`（構成の差し替え）。コマンドの書式は
[`operations.md`](operations.md)。

`--dry-run` では `RobotServer` が擬似値を作る（`lib/server_dryrun.py`）—— 各モータの状態を
`time.time()` ベースのサイン波で生成（**擬似値も `_measured_only()` を通る**）、ヘルス
スナップショットは全モータ・全バスを `ok` に上書き、シーケンスタスクは起動されるが実機同様に
開始合図を待って停止したまま。

---

## 2. ハードウェア構成

### モータ・アクチュエータ

| モータ | 個数 | ESC/ドライバ | CAN プロトコル |
|---|---|---|---|
| DJI M3508 | 2 | C620 ESC | CAN 2.0A Standard Frame |
| RobStride EDULITE 05 | 3 | 内蔵 | CAN 2.0B Extended Frame (29bit) |
| Damiao DM-S3519-1EC | 2 | DM3520-1EC ドライバ | CAN 2.0A Standard Frame |
| DC モータ / サーボ | 多数 | 自作モータドライバ | CAN 2.0A Standard Frame |
| 真空ポンプ（吸気 / 排気） | 2 | 自作モータドライバ（DC 基板の空き ch） | CAN 2.0A Standard Frame |
| 電磁弁（吸着パッド） | 6 | 自作モータドライバ（電磁弁基板 / STM32F303K8） | CAN 2.0A Standard Frame |

吸着系はサブハンドの機構。吸気ポンプは試合中回しっぱなしにし、吸着 / 解放は電磁弁だけで切り替える。
**ポンプは電磁弁基板ではなく DC 基板が動かす**（電磁弁基板の 6ch はすべて弁で埋まっており、
ポンプは起動電流が大きく `max_duty` で立ち上がりを抑えられる DC 基板が適する）。

### CAN バス構成（4 系統）

| バス（固定名） | CANable | 接続デバイス | ビットレート |
|---|---|---|---|
| `can_m3508` | #1 | M3508 × 2 | 1 Mbps |
| `can_edulite` | #2 | EDULITE 05 × 3 | 1 Mbps |
| `can_generic` | #3 | DC モータ / サーボ / 電磁弁（自作モタドラ） | 1 Mbps |
| `can_dm3520` | #4 | Damiao DM3520 × 2 | 1 Mbps |

**USB serial・bitrate・txqueuelen の実値は `config/can_buses.yaml` にしかない。**
インタフェース名を番号ではなく固定名にする理由と、DM3520 に専用バスが要る理由は
[invariants.md](invariants.md) の「CAN バス名は udev で個体固定する」「Damiao DM3520 は専用バス
（`can_dm3520`）に載せる」。`can_edulite` と `can_generic` は**メインハンドとサブハンドが
物理的に共有する**。CAN ID はバス単位でロボット横断に一意で、`tests/test_robot_sequences.py` の
静的テストが固定する。

#### セットアップ

セットアップと点検のコマンドは [`operations.md`](operations.md)。

PC 起動時は `cbc-can.service`（`Type=oneshot` + `RemainAfterExit=yes`）が
`setup_can.sh --wait 15` を実行する（`--wait` は USB 列挙の待ち時間で、**デッドラインは
全バスで共有する**）。USB 抜き差し時も udev の `RUN+=` で service が再実行される。
`setup_can.sh` は冪等で、up 済みのバスも一度 down してから再設定し（`ip link set type can` は
down 中しか受け付けない）、up 後に `ERROR-ACTIVE` を確認して成功を返す。udev ルールは
`can_buses.yaml` から生成されるので、**yaml を編集したら `sudo scripts/install.sh` の
再実行が必須**（`setup_can.sh` は yaml と配置済みルールのズレを警告し、`--strict` では失敗する）。

新しい CANable の serial は、対象の 1 個だけを挿した状態で
`udevadm info -a -p /sys/class/net/can0 | grep -m1 'ATTRS{serial}'` から採り、
`config/can_buses.yaml` の該当バスの `serial` へ記入して `install.sh` を再実行する。
`TBD` のままのバスは udev ルールに出力されず、`setup_can.sh` の対象からも外れる。

#### バスの状態と挙動

| インターフェースの状態 | `_create_bus()` | 受信ループ |
|---|---|---|
| 存在しない | `OSError: [Errno 19] No such device` で起動失敗 | — |
| 存在するが down | オープン成功。例外は出ない（起動ログに `operstate` 検出の ERROR が 1 行） | `bus.recv` が失敗し続けるが降りず、`rx_down` を立てて `BusHealth.DOWN` を出す |
| up | 正常 | 正常 |

### 自作モータドライバ基板（3 種）

**フレーム定義・状態フラグ・パラメータ ID・制御モードの単位・安全機構は
[`motor_driver_can_protocol.md`](motor_driver_can_protocol.md) が単一情報源。**
物理層は CAN 2.0A Standard Frame（11bit ID）/ 1Mbps、バスは `can_generic`。
バイト列をここに写さない。

| 基板 | MCU / CAN | ビルド系 | チャンネル | フィードバック |
|---|---|---|---|---|
| DC | UNO R4 Minima + 内蔵 CAN | PlatformIO (`uno_r4_minima`) | 3ch（duty のみ） | 状態フラグ 1 バイトだけ |
| サーボ #0 / #1 | Arduino Nano + MCP2515 | PlatformIO (`nano`) | 5 スロット（Servo / TouchSensor / Unused） | 状態フラグ + 現在角（DLC 3） |
| サーボ #2 | UNO R4 Minima + 内蔵 CAN | PlatformIO (`uno_r4_minima`) | 同上 | 同上 |
| 電磁弁 | STM32F303K8 + 内蔵 bxCAN | **CubeMX + CMake** | 6ch（GPIO の ON/OFF のみ） | 状態フラグ 1 バイトだけ |

- サーボは `firmware/servo/` の **1 プロジェクトで env 2 つ**（`nano` / `uno_r4_minima`）。MCU 差は `src/can_backend.h` に閉じる
- 共有ライブラリ `firmware/lib/MotorCan/` は `Arduino.h` も HAL も include しない純 C++ で、3 基板すべてが参照する。native テストが掛かるのはこの層
- デバイス ID は固定ビット分割（種別 2bit / 基板番号 3bit / スロット番号 3bit）。サーボ基板 `0x40`〜`0x7F` / DC 基板 `0x80`〜`0xBF` / 電磁弁基板 `0xC0`〜`0xFF`。基板番号 N のスロット k は `基底 + 8N + k`
- 版番号は `INFO` フレームで 1Hz 自己申告し、PC 側 `expected_firmware` と突き合わせる（`tests/test_firmware_version_sync.py` が 3 つの `config.h` と同梱の全 yaml を機械的に照合）

ピン配置の類推禁止・送信バッファ本数の差・状態フラグの組み立て・センサスロットの扱いは
[invariants.md](invariants.md) の §7 ファームウェア。

### 論理軸一覧

**メインハンド**（`config/main_hand.yaml` / `config/main_hand_positions.yaml`）:

| 論理軸 | モータ | ドライバ | バス | can_id | 役割 |
|---|---|---|---|---|---|
| `y_axis` | `y_axis_r` / `y_axis_l` | m3508 | can_m3508 | 1 / 2 | y 軸前後移動（ラックアンドピニオン）。**逆回転ペア** |
| `rotate` | `rotate_r` / `rotate_l` | edulite05 | can_edulite | 1 / 2 | エンドエフェクタ回転。**逆回転ペア** |
| `gripper` | 同名 | generic（サーボ #0 SV0） | can_generic | 0x40 | 開 / 閉 の 2 状態 |
| `wall_f` / `wall_r` | 同名 | generic（サーボ #0 SV1 / SV2） | can_generic | 0x41 / 0x42 | 初期 / 閉 / 開 の 3 状態 |
| `conveyor` | 同名 | generic（DC ch0） | can_generic | 0x80 | duty 軸。搬送方向はコート別 |
| （軸ではない） | `rotate_origin_sensor` | generic（サーボ #0 SV3） | can_generic | 0x43 | 原点タッチセンサ。**`sensors:` セクション** |

**サブハンド**（`config/sub_hand.yaml` / `config/sub_hand_positions.yaml`）:

| 論理軸 | モータ | ドライバ | バス | can_id | 役割 |
|---|---|---|---|---|---|
| `sub_y_axis` / `sub_lift` | 同名 | dm3520 | can_dm3520 | 0x01 / 0x02（MST 0x11 / 0x12） | 前後 / 昇降（ラックアンドピニオン直動） |
| `sub_gripper` | 同名 | generic（サーボ #1 SV0） | can_generic | 0x48 | 開 / 閉 |
| `valve_1`〜`valve_6` / `pump_vac` / `pump_blow` | 同名 | generic（電磁弁 #0 ch0-5 / DC ch1・ch2） | can_generic | 0xC0〜0xC5 / 0x81 / 0x82 | `on_off` ×6 / duty ×2 |

`sub_y_axis` / `sub_lift` は**独立 2 軸**で左右直結ペアではない（`sync_tolerance` を持たない）。
軸名に `sub_` を付けるのは、統合動作確認が両ハンドの位置定数を `PositionTable.merged` で
1 つにまとめるため（軸名が重なると起動ごと落ちる）。

---

## 3. ディレクトリとモジュール構成

```
cbc2026_team3/
├── main.py                     # 配線と起動・後始末（§1 の 4 段）
├── pyproject.toml
├── docs/                       # 一覧は §13
├── config/                     # §6
├── scripts/                    # §10
├── lib/                        # 共通ライブラリ（両ロボットで共有）
├── sequences/                  # シーケンス定義
├── firmware/                   # 自作モータドライバのファーム
├── web/                        # Vite + React の操作 UI
└── tests/                      # §11
```

### `lib/` のレイヤ

依存は上から下へ一方向。`lib/axis_sync.py` が最下位で、上位を import しない。

```
配信・受理    server.py / ws_hub.py / server_motor_check.py / server_dryrun.py
制御権と手順  manual.py / sequence/engine.py / sequences/*.py
軸への指令    sequence/motors.py / sequence/homing.py / sequence/positions.py
周期タスク    control/position_loop.py / sync_monitor.py / limit_monitor.py /
              target_refresh.py / trajectory.py ─ periodic.py / feedback.py /
              sync_guard.py / pid.py
CAN           can_manager.py ── drivers/{base,m3508,edulite05,dm3520,generic}.py
最下位        axis_sync.py / motion_guard.py / config_schema.py / health.py / match_state.py / commands.py
```

| モジュール | 持つもの |
|---|---|
| `axis_sync.py` | 左右直結ペアの単位換算とずれ判定（`MotorSpec` / `SyncGroup`）。**偏差監視の 3 段すべてがここの `violation()` を呼ぶ** |
| `motion_guard.py` | 指令を出してよいかの判断（`MotionGuardSpec` / `MotionGuard`）。可動端インターロック・跳躍量・トルクだけを持ち、送信も状態も持たない。`axis_sync.py` と同じ最下位層。**可動端の判定 `check_limit()` は指令経路と `LimitMonitor` の両方がここを呼ぶ**。`LimitSpec` は**向きごとに何本でも**持ち（左右直結ペアは同じ端に 1 本ずつ）、1 本でも押されて／読めていなければその向きを塞ぐ。`SensorSuspension` は零点確定の整列段だけがセンサを外す口（歯止めが読む口にだけ掛ける覆い） |
| `can_manager.py` | SocketCAN 複数バス管理。受信ループと `_dispatch_frame`、励磁シーケンス、ヘルス |
| `commands.py` | WS コマンドの語彙（名前・許可フェーズ・緊急停止時の可否・ハンドラ・拒否経路）の単一情報源 |
| `config_schema.py` | yaml の検証付き読み込み。**しきい値の既定値もここだけが持つ** |
| `drivers/base.py` | `MotorDriver` 基底 / `MotorState` / `ControlMode` / `TelemetrySupport`（測定可否の宣言） |
| `control/periodic.py` | 周期タスクの土台（`PeriodicTask` / `PausablePeriodicTask` / `LogThrottle`）+ 実周期の計測 |
| `control/feedback.py` / `sync_guard.py` | フィードバック鮮度の判定（`FeedbackFreshness`。未受信は異常にしない）/ 左右直結ペアの局所保護（`SyncGuard`。判定とラッチだけ） |
| `control/pid.py` / `trajectory.py` | モータ非依存 PID（測定値微分 / conditional integration / デッドバンド）/ 台形速度プロファイル |
| `control/position_loop.py` | M3508 の PC 側位置制御ループ（バス単位・200Hz） |
| `control/sync_monitor.py` | 左右ペア軸のずれを常時監視（50Hz）。超過で全体緊急停止 |
| `control/limit_monitor.py` | 移動中の可動端インターロック（50Hz）。目標へ向かう先の端が押されていたら実測位置を目標へ書き直して止める。判定は `MotionGuard.check_limit`。観測周期より狭い ON 区間は接触の累計で拾い、止めた回数（`intervention()`）を `move_to` へ渡す |
| `control/target_refresh.py` | `GenericTargetRefresher` / `QueryDrivenTargetRefresher`（ともに 20Hz） |
| `sequence/positions.py` | 位置定数 yaml の読み込み・単位換算・論理軸の解決（`PositionTable` / `AxisSpec`） |
| `sequence/homing.py` / `motors.py` / `engine.py` | 零点確定（`HomingRunner`。センサ読みと原点確定は注入）/ `MotorHandle` と `AxisHandle` / `@step` ベースのシーケンスエンジン |
| `manual.py` / `tuning/metrics.py` | 手動操縦（`OperationMode` / `ManualController`。軸単位でしか指令しない）/ ステップ応答の指標算出（呼び出し元は `scripts/tune_y_axis.py` だけ） |
| `health.py` / `match_state.py` | ヘルスの語彙と集約（`worst_bus_health`）/ フェーズ・コート・指差喚呼・試合時間（`ALL_ROLES` はここだけ） |
| `server.py` / `ws_hub.py` | aiohttp。フェーズ / 制御権 / 配信内容の組み立て / WS クライアント集合と**唯一の配信経路**（`WsHub`） |
| `server_motor_check.py` / `server_homing.py` / `server_dryrun.py` | 動作確認の統括（可否判定の単一情報源）/ 零点合わせ単独実行の統括（宛先のロボットと軸を持つ）/ dry-run の擬似値（見栄えの値しか作らない） |
| `logging_setup.py` | ログの体裁の単一情報源。`main.py` が起動時に 1 回だけ呼ぶ |

`lib/control/` と `lib/tuning/` の `__init__.py` は**再エクスポートを持たない**。

### `sequences/`

`main_hand.py` / `sub_hand.py` は `main._load_sequence` が `sequences.<robot_name>` として
動的 import する。`motor_check.py`（**両ハンド統合**の動作確認）はロボットではないので
動的 import の対象外で、`main.py` が静的 import する。機体そのものの定義（モータ構成・
ドライバ種別・CAN ID）は `config/<robot>.yaml` にあり、`sequences/` の中身は手順だけである
（旧名 `robots/` はこの中身とずれていた）。

### 公開 API（層をまたぐ参照はここだけを通る）

| API | 何を出しているか |
|---|---|
| `CANManager.motors` | `MappingProxyType`（宣言順） |
| `CANManager.bus_names` | バス名のタプル（**`can.Bus` そのものは渡さない**） |
| `CANManager.last_feedback_at(motor)` | 受信時刻 / `None`（鮮度判定は `FeedbackFreshness` が行う） |
| `RobotServer.handle_command` | 経路非依存のコマンド受理口。WS も内部の安全機構も同じ 2 段ゲートへ合流する |
| `Sequence.steps` | 宣言順のステップ表（`tuple`）。`_steps` はクラス属性なので実体の list は渡さない |
| `WsHub.fanout` / `WsHub.close_all` | 全クライアント配信と一斉切断。**配信経路はこの 2 つだけ** |

`tests/test_server_encapsulation.py` が `名前._private` の形と
`asyncio.get_event_loop()` を AST で禁止する。

### `firmware/`

```
firmware/
├── README.md              # 通電前の要確認項目・デバイス ID・安全既定値・テストの方針
├── common.ini             # 共通部のみ（MCU が違うので platform/board は各 ini）
├── lib/MotorCan/src/      # 3 ファーム共有。Arduino / HAL 非依存の純 C++
│     MotorCanProtocol（CAN ID / デバイス ID のビット分割 / int16 固定小数点）
│     MotorCanRouter（宛先判定・DIP 読み出し）/ MotorLoopTimer（PeriodicTimer）
│     MotorSafety（緊急停止ラッチ + ウォッチドッグ + 物理停止入力）
│     MotorTxHealth（TxFailCounter）/ SerialLineBuffer（デバッグシリアルの行組み立て）
│     DcChannel / ServoMotion / ServoChannel / SolenoidChannel（1ch 分の結線）
├── test/                  # native 環境の Unity テスト（§11）。全プロジェクトが test_dir で共有
├── dc_motor/              # include/config.h + src/main.cpp
├── servo/                 # env 2 つ（nano / uno_r4_minima）。MCU 差は src/can_backend.h
└── solenoid/              # solenoid.ioc（CubeMX）+ include/config.h + src/app.cpp
```

**native テストは 2 プロジェクトが `test_dir` を共有するのでどちらか一方で足りるが、実機
ビルドは 3 env とも必要**（dc_motor / servo の `nano` / servo の `uno_r4_minima`）。電磁弁だけ
CMake。コマンドは [`operations.md`](operations.md)。

**native テストはどちらか一方で足りる。実機ビルドは 3 つとも必要**（`main.cpp` と
`config.h` が別物のため）。`arm-none-eabi-gcc` は 11 以降が要る（詳細は `firmware/README.md`）。

### `web/src/`

```
web/src/
├── App.tsx           # applyLegacyHashRedirect() → createBrowserRouter()（この順序に依存）
├── routes.tsx        # /monitor /main-hand /sub-hand
├── index.css         # Tailwind + daisyUI カスタムテーマ cbc（組み込みテーマは使わない）
├── layouts/RootLayout.tsx   # WS 接続・Provider・外枠（外枠は memo した AppShell）
├── context/  ModalContext.tsx（表示中モーダル数 = ホットキー抑止の判定元）
│            RobotContext.tsx（useRobotStates / useRobotStatus / useRobotCommands）
├── hooks/    useWebSocket.ts（接続・再接続・接続先切替のみ。メッセージを解釈しない）
│            useRobotSocket.ts（protocol + robotReducer + useWebSocket を束ねる）
│            useWsUrl / useHotkeys / useHoldKey / useHoldRepeat / useMotorCheck
│            useArmedPress.ts（二度押しで実行する操作 = 試合開始・終了）
├── lib/      # 最下層。hooks/ を import しない。判定を持つものは §9 の表
│            protocol.ts / robotReducer.ts / phase.ts / healthVerdict.ts /
│            sequenceStatus.ts / motorCheckStatus.ts / syncVerdict.ts /
│            checklistGroups.ts / tabs.ts / robots.ts / time.ts / tone.ts /
│            cx.ts / wsUrl.ts
├── pages/  Dashboard.tsx / RobotControl.tsx
├── test/   setup / mockWebSocket / robotContext + ws-contract.json / wsContract.test.ts
└── components/  # 分割軸は「誰が描くか」。直下にファイルは置かない。役割は §9 の表
      shell/（RootLayout が全画面へ出す外枠）/ monitor/（Dashboard 専用）/
      operator/（RobotControl 専用）/ motorcheck/ / diagnostics/ /
      ui/（自前プリミティブ: Page / Panel / Section / Button / StatusBadge /
           Kbd / Icon / Modal）
```

barrel（`index.ts`）は作らず、常に実ファイルまで指す。コマンドは `cd web &&`
`pnpm install` / `pnpm dev` / `pnpm build` / `pnpm test:run` / `pnpm check`。

---

## 4. 制御レイヤ

### PID がどこで閉じているか

| モータ | 位置ループの所在 | PC が送るもの |
|---|---|---|
| RobStride EDULITE 05 | **モータ内蔵ドライバ**。起動時に `run_mode=位置` と `PARAM_LOC_KP`（既定 30.0、config の `position_kp`）を書き、実測角を保持目標に書いてから励磁する | 目標角のみ（`PARAM_LOC_REF` への float 書き込み） |
| Damiao DM3520 | **ドライバ内蔵**（Position Velocity Mode = 位置 → 速度 → 電流の三重ループ） | `p_des` [rad] / `v_des` [rad/s] の float32 2 つ |
| 自作モタドラ（DC / サーボ / 電磁弁） | **閉じていない。** DC は duty をそのまま出し、サーボは角度補間だけ、電磁弁は GPIO の ON/OFF | 目標値のみ（`SET_TARGET` の Byte0 が制御タイプを毎通運ぶ） |
| DJI M3508 (C620) | **電流ループのみ ESC 内。位置ループは PC 側**（`lib/control/position_loop.py`、200Hz） | 電流指令（`0x200` フレーム。4 モータ分を 1 通に束ねる） |

config の `pid: null` が「ドライバ側で制御していて PC 側 PID を持たない」の単一の表現で、実際に
位置制御ループへ載せるかは `main._build_position_loops` がドライバ種別（M3508 だけ）で決める。

### CAN 受信ループ

バス 1 本につき `_receive_loop` が 1 つ回り、受け取った 1 通を `_dispatch_frame` が宛先モータへ
配る。**失敗の封じ込め粒度はモータ 1 台**で、`matches_feedback`（宛先判定）と `update_state`
（デコード）をそれぞれ別に囲う。

| 事象 | 扱い |
|---|---|
| `matches_feedback` が例外 | そのモータを飛ばして次のモータへ |
| `update_state` が例外 | その 1 通を捨てる。**`_last_rx_at` は更新しない**。`rx_error_count` に積む |
| `bus.recv` が例外 | 降りずに待って呼び直す。`rx_down` を立て、ログは `LogThrottle` へ通す |
| `asyncio.CancelledError` | 素通し（`Exception` しか捕まえない） |

`fileno()` を持つバスは fd を `loop.add_reader` に載せ、起きたら滞留を `recv(0)` で出し切る
（`_ReadableFd`。復帰待ちのあいだは `remove_reader` で監視を外す）。`fileno()` を持たないバス
（`--dry-run` の virtual バス）だけがエグゼキュータ経由へ落ちる。ブロッキング呼び出しの実行口は
`BlockingRunner` として注入する。これらの扱いの理由は [invariants.md](invariants.md) の
「受信は 1 通ごとにエグゼキュータへ往復してはならない」「受信フレーム 1 通の失敗を、そのバス
全体の失敗にしない」「`bus.recv` の失敗で受信ループを降ろしてはならない」。

### M3508 の位置制御（PC 側 PID）

`lib/control/pid.py`（モータ非依存の PID: 測定値微分 / conditional integration / デッドバンド）と
`lib/control/position_loop.py`（`M3508PositionLoop`。**CAN バス単位**）。

| 項目 | 中身 |
|---|---|
| 周期 | 既定 200Hz（`DEFAULT_INTERVAL_S = 0.005`）。`dt` は毎周期 `time.monotonic()` の実測差分 |
| 送信 | 全モータ分の電流を `M3508Driver.encode_current_frame()` で 1 フレームに束ね、1 周期 1 通 |
| 多回転 | `decode_feedback()` の `position` は 0〜360 のまま。累積角は `update_state()` がアンラップして `multi_turn_position`（deg）で公開。窓の長さは**フレーム自身のタイムスタンプ**で測る |
| 到達判定 | `_observed_for(POSITION)` を `multi_turn_position` へオーバーライド。`default_tolerance(POSITION)` は共通既定 1deg × `GEAR_RATIO`（3591/187 ≒ 19.2）で出力軸 1deg に揃える |
| 目標値の流れ | `MotorHandle.target_sink` に `M3508PositionLoop.target_sink(name)` を差し込む。`ControlMode.CURRENT` は PID を通さず素通し（ホーミングの押し当て用）、VELOCITY / DUTY は `ValueError` |
| 原点の張り直し | `set_origin_here()` は同期グループ全員を `await` を挟まず 1 回で確定（判断は `_paired_with()` 1 箇所） |

**安全側の挙動**:

| 条件 | 挙動 |
|---|---|
| 緊急停止中（`is_estop_active`） | 電流 0 + PID リセット + 目標解除 + プロファイル破棄 |
| フィードバック途絶（`health.feedback_timeout_ms` 超過） | 電流 0 + PID リセット + プロファイル破棄 |
| 相方（直結ペア）の異常 | グループ全員を電流 0 + プロファイル破棄 |
| 周期処理で例外 / 周期が飛んだ | ログを残して 0 電流を送りループは継続 / PID に渡す `dt` を `DEFAULT_MAX_DT_S`（50ms = 制御周期の 10 倍）で頭打ち |
| 一時停止中（`pause()`） | **1 通も送らない**。緊急停止は状態のみ反映。`resume()` は目標を残して PID と `_last_tick` をリセット |

**PID ゲインの config スキーマ**（`config/<robot>.yaml` の `motors.<name>.pid`）:

| キー | 既定値 | 意味 |
|---|---|---|
| `kp` / `ki` / `kd` | 2.0 / 0.0 / 0.0 | 累積角 [deg] → 電流指令 [counts] |
| `integral_limit` | `null` | 積分項の出力寄与上限 [counts]（`null` で無制限） |
| `dead_band` | 1.0 | 偏差の不感帯 [deg] |
| `output_limit` | 2000 | 電流指令の絶対値上限 [counts]。`CURRENT_MAX`(16384) で頭打ち |

ゲインは起動時に読んで以後動かない。実行中に差し替える経路は無く、実機で詰め直すのは
`scripts/tune_y_axis.py` + `config/bench/y_axis_tuning/`（上書きはプロセス内に閉じる）。
`_DEFAULT_PID` は同梱のどの config からも到達しない（全 M3508 が `pid:` を明示している）。

### 台形速度プロファイル（`lib/control/trajectory.py`）

位置指令の軸に `axes.<軸>.motion`（§6）を書くと、最終目標をそのまま PID へ入れず、
**速度・加速度で制限した中間目標**を毎周期生成して PID へ渡す。書かない軸は従来どおり
ステップ入力。

| 設計点 | 内容 |
|---|---|
| 持ち方 | **モータ単位**（グループ単位にしない）。`add_motor` の引数ではなく後付けで渡す |
| 換算 | `abs(scale)`（速度・加速度の制限は向きを持たない）。`position_loop.py` は指令単位しか扱わない |
| 起点 | **前周期に出した中間目標**。実測から張り直すのは位置制御へ入る最初の 1 周期だけ（`_anchor_profile`）。破棄は `profile_anchored` フラグ 1 つに集約 |
| 減速判定 | `_stoppable_velocity` が「この周期に出してよい速度の上限」を離散時間の停止距離から厳密に解く（連続時間の `v²/(2a)` は使わない） |
| 速度 FF | `PIDController.update(feedforward=…)` へ同期補正と加算して渡す（PID の外で足さない） |
| 起動時検証 | `MotionSpec.duration_for()`（閉じた式）で最長移動の所要を見積もり、`timeout_s` が足りなければ起動拒否。三角プロファイル（`v_max` に達しない短距離）は台形の式で見積もらない |
| 書けない軸 | `command_mode` が POSITION でない軸（`duty` / `on_off`）。`max_velocity` と `max_acceleration` は対で必須 |

**起動ログに 3 値（`max_velocity` / `max_acceleration` / `velocity_ff`）を必ず出す** ——
実行中に変更できず UI にも配信されないので、起動ログが唯一の読み口である。

### 左右ペア軸の保護（偏差監視の 3 段）

機構的に直結した左右ペアは 4 組（`y_axis` = M3508 ×2 / `rotate` = EDULITE ×2 /
`sub_rotate`・`sub_pitch` = サーボ ×2）。位置定数 yaml の `motors:` で 1 論理軸に複数モータを
束ね、逆回転は `scale` の符号で表す。**偏差監視に載るのは `sync_tolerance` を書いた軸だけ**で、
現状は `y_axis` と `rotate` の 2 組（サーボの 2 組が書いていない理由は
`docs/mechanism_handoff.md` §2）。

**判定と単位換算は `lib/axis_sync.py` に一本化してあり、3 段とも `SyncGroup.violation()` を呼ぶ。**

| 段 | 効く軸 | 頻度 | debounce | ラッチ | 効果 |
|---|---|---|---|---|---|
| `sequence/engine.py` の `move_to`（`AxisHandle.sync_violation`） | `y_axis` / `rotate` | move_to 完了時 1 回 | なし | なし | `AxisSyncError` でシーケンス停止 |
| `control/position_loop.py`（`_check_deviation` / `SyncGuard`） | **`y_axis` のみ** | 200Hz | なし | あり | グループ全員を電流 0 |
| `control/sync_monitor.py`（`_check_group`） | `y_axis` / `rotate` | 50Hz | 2 サンプル | あり | **全体緊急停止** |

**200Hz の段に載るのは全メンバが同じ位置制御ループに載る組だけ**（`main._attach_sync_groups`。
外れた組は起動ログに「位置制御ループ外（`SyncMonitor` のみで監視）」と出る）。`rotate` は
EDULITE なので M3508 の位置制御ループを持たず、この段には載らない。`move_to` の段は完了時に
しか見ないので、**手動操縦中と零点確定中は 1 度も発火しない**（零点確定は
`AxisHandle.set_target_value` を直に呼ぶ）。

`lib/axis_sync.py` の公開 API は、`MotorSpec.to_command` / `to_value` / `to_tolerance`
（人間の単位 ⇄ 指令単位の換算。`to_tolerance` は `abs()` を掛ける）、`SyncGroup.deviation()`
（人間の単位へ逆換算した位置の `max - min`）、`SyncGroup.violation()`（唯一の境界。比較対象が
2 個未満 = 途絶・未受信なら超過とみなさない）、`SyncGroup.corrections()`（同期補正の操作量）。
偏差監視は 3 段とも**止めるだけ**で、加えて**フィードバック途絶をペア単位で判定**する
（片方が stale なら両方を電流 0）—— ただしこれを持つのは `SyncGuard` だけなので、
**効くのは `y_axis` のみ**である。`SyncMonitor` は途絶したメンバを判定から外すだけで、
比較対象が 2 個未満になれば `violation()` は `None` を返し連続カウントも捨てる ——
`rotate` は片側が途絶するとずれ判定そのものが成立しなくなる。**途絶を見て電流 0 へ落とす
経路は `M3508PositionLoop` にしかない**ので、残った側は最後の目標を保持したまま駆動を続ける。

#### 同期監視（`lib/control/sync_monitor.py`）

対象は `sync_tolerance` を持つ全軸（= `AxisSpec.sync_group`）で、50Hz / 連続 2 サンプル
超過で発報。フィードバックが `feedback_timeout_ms` より古いモータは判定から除外する。
超過時は `on_violation(axis_name, deviation)` を呼び、`main.py` が
`asyncio.create_task(server.activate_e_stop(reason=…))` へ接続する（タスクは GC 回避のため
`main()` の集合で強参照を保持）。ロボットごとに、グループが 1 つ以上あるときだけ生成する。

#### 同期補正（クロスカップリング）

駆動中にずれを縮める唯一の経路。`SyncGroup.corrections()` が「グループ平均へ引き戻す向き」の
操作量を各モータの指令単位で返し、`M3508PositionLoop` が `PIDController.update(feedforward=…)`
へ渡す。ゲインは `axes.<軸>.sync_kp` / `sync_limit`（§6。既定は `sync_kp: 0.0` = 補正なし）。
補正を出さないのは①電流 0 に落とす周期 ②全員が位置制御中でない周期 ③左右が同じ軸位置を
目標にしていない周期（零点確定の整列段。`SyncGuard.skewed_groups()`）の 3 つで、判断はグループ
単位。設計の根拠は [invariants.md](invariants.md) の「保護は止めるだけ。駆動中にずれを縮めるのは
`sync_kp` だけである」。

### 周期タスクの共通土台（`lib/control/periodic.py`）

5 つの周期タスクは `PeriodicTask`（送信経路を奪い合いうるものは `PausablePeriodicTask`）を継承し、作法を揃える。

| タスク | 周期 | 対象 | 送るもの |
|---|---|---|---|
| `M3508PositionLoop` | 200Hz | バス上の全 M3508 | `0x200` 電流指令フレーム（1 周期 1 通） |
| `SyncMonitor` | 50Hz | `sync_tolerance` を持つ全軸 | 送信しない（監視のみ） |
| `LimitMonitor` | 50Hz | `guard.limits` を持つ全軸 | 発火した軸にだけ「その場の実測位置」を目標として 1 通（**指令の単位のまま**書き戻す。値へ換算して戻すと往復の丸め誤差で入口の歯止めに拒否される） |
| `GenericTargetRefresher` | 20Hz | generic ドライバのモータ | `SET_TARGET` の再送 |
| `QueryDrivenTargetRefresher` | 20Hz | EDULITE 05 + DM3520（`_QUERY_DRIVEN_DRIVERS`） | 目標値 or `idle_target_value()` のラッチ値 |

共通の作法は、二重 `start()` は `RuntimeError` / `stop()` は `cancel()` せず停止イベントで
降ろす（待ちは最大 1 周期。`_on_run_exit()` で 0 電流フレームを送るタスクがある）/
**周期は次回起床時刻を絶対時刻で管理する**（1 周期以上遅れたら取り戻さず位相を捨てて
数え直す）/ 例外は `LogThrottle` に落としてループを継続する /
**実周期の乱れを測る**（公称の 1.5 倍を超えた回数と最悪値だけを O(1) で積み、集計 1 行を
`match_finish` で journal へ INFO。`match_start` は前縁リセットだけで、**画面にも WS 配信にも
出していない**）。

判定そのものも制御層で共有する —— `FeedbackFreshness`（`control/feedback.py`。この実測値は
まだ信じてよいか。**未受信は異常にしない**）と `SyncGuard`（`control/sync_guard.py`。左右直結
ペアをこの周期で電流 0 に落とすか）。

### 2 種類の目標値再送

自作モタドラのファームは 500ms 自分宛の `SET_TARGET` が来ないと出力を止め（仕様書 §5.1）、
EDULITE 05 / DM3520 は自分の CAN ID 宛のフレームを受けたときにしか状態を返さない。この 2 つを
別のタスクが受け持ち、**目標を持たないモータの扱いが正反対**である。

| | `GenericTargetRefresher` | `QueryDrivenTargetRefresher` |
|---|---|---|
| 対象 | generic ドライバのモータ | EDULITE 05 / DM3520（`_QUERY_DRIVEN_DRIVERS` が単一情報源） |
| 送る理由 | ファームのコマンドウォッチドッグ（500ms）を養う | ①フィードバックを引き出す ②ドライバの `TIMEOUT` レジスタを養う |
| 目標が無いとき | **送らない** | **送る**（`idle_target_value()` を**ラッチした値**） |
| 緊急停止中 | 送らない（保持目標ごと `clear_targets()` で捨てる） | **送る**（ただしラッチを取らず毎回測り直す） |
| 動作確認中 | 止めない | 止めない |
| 終了時 | 停止指令は送らない（ファームのウォッチドッグに任せる） | 同左 |

M3508 だけが再送不要（位置制御ループが 200Hz で送り続け、C620 が自発的にフィードバックを返す）。

### Damiao DM3520 の扱い

**ID 帯**（本機は受信 ID の**下位 8bit だけ**を見て自分宛かを判定する）:

| フレーム ID | 意味 |
|---|---|
| `0x000 + ID` | MIT モードの指令 / 特殊コマンド（enable `0xFC` / disable `0xFD` / set zero `0xFE`） |
| `0x100 + ID` | 位置速度モードの指令（`p_des` / `v_des` の float32 2 つ） |
| `0x200 + ID` | 速度モードの指令（`v_des` の float32 1 つ） |
| `0x7FF` | パラメータ読み書き（対象は D0/D1 の CAN ID で選ぶ） |
| `MST_ID` | フィードバックとパラメータ応答（本機 → PC） |

**起動手順**（EDULITE 05 と同じ形）: ①`disable`（`0xFD`）→ ②`CTRL_MODE`（レジスタ `0x0A`）
へ制御モードを書く（**フラッシュへ保存されない**ので毎起動書く）→ ③（`set_zero_on_start`
なら）`set zero`（`0xFE`）→ ④**実測角を `p_des` として書いてから** `enable`（`0xFC`）。
`requires_fresh_feedback_for_activation()` が `True` を返すのは④のため。
**パラメータ応答の判別**（フィードバックとして取り込まないため）は `D0`/`D1` = 対象 CAN ID
（リトルエンディアン）かつ `D2` ∈ {`0x33`, `0x55`}。

**実機のレジスタ値**（config の `p_max` / `v_max` / `t_max` はこれと一致させる。ずれると
値が比例倍で読める）:

| レジスタ | 値 | 備考 |
|---|---|---|
| `0x15` PMAX | 12.5 rad | `scale` 換算で ±225mm |
| `0x16` VMAX | 200.0 rad/s | **定格ではなくマッピングレンジ**（無負荷最高は 435rpm ≒ 45.6rad/s） |
| `0x17` TMAX | 10.0 Nm | ピークトルクは 7.8Nm |
| `0x14` Gr | 19.2032 | = 3591/187 |
| `0x09` TIMEOUT | 0 | 通信途絶保護は無効。20Hz 送信の意義はフィードバック引き出しの側にある |

実機の ID は `sub_y_axis` = ESC_ID `0x01` / MST_ID `0x11`、`sub_lift` = `0x02` / `0x12`。
検査は `lib/config_schema.py` の `_check_dm3520_master_id_collisions` が起動時に行う。

### 零点確定（ホーミング）

`lib/sequence/homing.py` の `HomingRunner` が持ち、**動作確認シーケンスの最初のステップ**として走る。
軸を並べて順に回す段は同じファイルの `run_homing()` にあり、**通し実行と単独実行
（`homing_start`。下の「零点合わせだけを走らせる」）が同じ 1 本を通る**。
設定は `*_positions.yaml` の `axes.<軸>.homing`（§6。軸の機構的性質であって動作確認固有の
値ではない）で、`sensor`（`sensors:` に登録されていること）/ `direction` / `step` /
`settle_s` / `search_distance`（必須）を持つ。

| 段 | 内容 |
|---|---|
| 事前確認（5 つ。どれも 1 歩も動かさずに落ちる） | 位置指令の軸か / 原点を確定する手段があるか / モータの励磁 / センサの鮮度 / **対象軸モータの鮮度** |
| 離脱 | 既にセンサに触れているなら、離れるまで動かす。判定は**現在値**（ラッチを使わない） |
| 探索 | 毎ステップ `AxisHandle.observed_value()` を読み直して `commanded = observed + direction*step`。到達判定は**接触の累計**（`GenericDriver.sensor_contact_count`。読んでも減らない単調カウンタで、探索開始直前に基準値を取り直す） |
| 停滞判定 | `step/2` 未満が 3 歩連続で `HomingError` |
| 整列段（`homing.sensors` を書いた軸のみ） | まだ当たっていない側のモータだけを進める。**この段のあいだだけその軸の原点センサを可動端の歯止めから外す**（`SensorSuspension`。外さないと、既に押された 1 本を見た歯止めが指令の入口でも 50Hz 監視でも拒否して必ず失敗する） |
| 検出後 | その場の実測位置を目標に送り直してから原点確定（`_stop_here`。**指令の単位のまま**書き戻す。値へ換算して戻すと往復の丸め誤差で入口の歯止めに拒否される） |
| 原点確定 | `set_group_origin_here`（グループ単位でしか行わない） |

**原点を確定する手段は 2 つ**で、可否はドライバ自身の `supports_origin_capture()` が答える
（`main._make_origin_resolver` にドライバ種別を書き写さない）:

| 手段 | 対象 |
|---|---|
| PC 側位置制御ループの原点張り直し | M3508 |
| ドライバへの `SET_ZERO` | EDULITE 05（`deactivation_steps()` と `origin_capture_steps()` の両方を持つ） |

**手段が無い軸は探索を始める前に落ち、起動ログにも `ERROR` で出る。**
どの軸で現在有効かは [`checks_and_health.md`](checks_and_health.md) の
「零点確定（ホーミング）」節が正。

### 緊急停止の経路

**発動**（`activate_e_stop(reason=…)` が唯一の入口。`e_stop` コマンドも同じ経路）と**解除**（`e_stop_release`）:

| 発動 | 解除 |
|---|---|
| 動作確認を abort（状態を問わず） | 同期ずれラッチを解除（`_reset_sync_latches`） |
| 停止理由を保持（保持済みなら上書きしない） | 停止理由を破棄 |
| M3508 へ全スロット 0 の電流指令（`send_stop_frame`） | `_e_stop_active` を落として配信 |
| ドライバ固有の停止フレーム（EDULITE） | ①全バスへ `0x0FF` のブロードキャスト解除 |
| 全バスへ `0x0FF` ブロードキャスト（自作モタドラ） | ②管轄内の自作モタドラへ個別のラッチ解除（**中断しない**） |
| 目標値再送の保持目標を破棄（`clear_targets`） | ③励磁（`activate_motors`。中断あり） |
| 全シーケンスを停止（保留中の開始要求も破棄） | ④`_board_e_stop_ignore_before` を①②③の後に置く |

**検知経路**は 3 つ:

| 発生源 | 経路 |
|---|---|
| 操縦者 | UI の EMG STOP → `e_stop` コマンド（`reason` は付かない） |
| 同期監視 | `SyncMonitor.on_violation` → `activate_e_stop(reason=…)` |
| 基板 | `FEEDBACK` の緊急停止ビット → `_detect_board_e_stop` → `activate_e_stop(reason=…)`。判定は `_board_e_stop_ignore_before` より後に届いたフィードバックに限る |

物理非常停止は DC 基板の `REF` が受けてファーム側でラッチされ、解除は CAN の `E_STOP` 解除
フレームだけ（電磁弁基板に `REF` 入力は無い）。**再励磁**（`reenergize_motors`）は励磁が
落ちたモータを機体を止めずに戻すコマンドで、無励磁のモータ（と直結ペアの相方）だけ目標ラッチを
剥がしてから `activate_motors(only=…)` で絞って励磁する。100ms〜1.5 秒かかる別タスクで走り、
そのあいだ `safety.reenergizing` が立つ（在飛判定は `RobotServer._is_reenergizing` が
`_reenergize_tasks` / `_reactivate_tasks` の両方を畳んで答える）。

---

## 5. シーケンス

### エンジン（`lib/sequence/engine.py`）

`@step` デコレータでメソッドを宣言順のステップ表にする。`__init_subclass__` が
`_steps`（クラス属性）を組み立て、`Sequence.steps` が `tuple` で公開する。

```python
from lib.sequence.engine import Sequence, step

class PickAndPlace(Sequence):
    @step("初期位置へ移動")
    async def move_to_home(self):
        # {軸名: 位置名} を渡すと、換算・指令・到達待ちまで move_to が面倒を見る
        await self.move_to({"y_axis": "home", "rotate": "home"})

    # 失敗すると機構破損に直結する動作は必ず操縦者の許可を待つ
    @step("ハンド閉じる", require_trigger=True)
    async def close_hand(self):
        await self.move_to({"gripper": "closed"})
```

| 要素 | 中身 |
|---|---|
| `run_forever()` | 常駐ループ。開始要求を待って停止したまま起動する。通常停止後の先頭への巻き戻しもここが持つ |
| `run()` | 1 周ぶんの実行。冒頭で停止イベントを `clear()` する |
| `require_trigger=True` | そのステップの手前で必ず止まり、操縦者の `trigger` を待つ（例外は無い） |
| `court` | `move_to` が `PositionTable` へ自動で渡す |
| `bind_motors()` / `bind_positions()` | `main.py` が `MotorGroup` と `PositionTable` を注入する |
| `restrict_to_axes()` | 構成に無い軸のステップを除外する判定の 1 箇所（`@step(axes={…})` の宣言と対） |
| `discard_pending_start()` | 未処理の開始要求を捨てる（緊急停止が呼ぶ） |
| ステップログ | エンジンが `[main_hand] 2/22 …` の形で 1 箇所だけ出す |

**例外**:

| 例外 | 契機 |
|---|---|
| `SequenceTimeoutError` | 軸ごとの `timeout_s`（既定 5.0s）内に到達しなかった |
| `AxisSyncError` | 到達後に `SyncGroup.violation()` が偏差を返した |
| `EStopActiveError` | 緊急停止中に `MotorHandle.set_target` が呼ばれた |
| `PositionLookupError` | 位置定数表に軸名・位置名が無い |
| `HomingError` | 零点確定の事前確認・探索・停滞判定の失敗 |

`run()` は例外を握って `break` するので、止まった位置が UI から分かり、操縦者が原因を除去して該当ステップへジャンプできる。

### `Sequence.move_to` の責務

| 項目 | 決定 |
|---|---|
| 単位換算 | `PositionTable`（yaml の `axes`）が担当。シーケンスに数値は現れない |
| コート | `self.court` を自動で渡す。スカラー値の位置はコートに依存しない |
| 到達待ち | 軸ごとに `wait_reached(tolerance, timeout)` を**並列**実行 |
| タイムアウト | 軸ごとの `timeout_s`。`move_to(..., timeout=)` で上書き可 |
| タイムアウト時 | `SequenceTimeoutError` を送出 |
| 可動端で曲げられたとき | 指令の前後で `LimitIntervention.count`（注入。既定は「保護なし」）を比べ、増えていれば理由を添えて `SequenceTimeoutError`。到達判定は保護の書き戻しで必ず成立するので、これが無いと軸が途中に居るまま次のステップへ進む |
| 指令値の後始末 | **クリアしない**（昇降軸で保持トルクを失うとワークごと落下する） |
| 呼び方 | 複数軸は 1 回の `move_to` へまとめて渡す（分けると待ちが軸の数だけ直列に積み上がる） |

### `AxisHandle`（`lib/sequence/motors.py`）

1 論理軸（1〜N モータ）への指令と到達待ちをまとめる薄いラッパ。状態を持たないので `move_to` のたびに生成してよい。

| メソッド | 責務 |
|---|---|
| `set_target_value(commands)` | `{モータ名: 指令値}` を `command_mode` で**同時に**送る（`asyncio.gather`）。**唯一の指令経路** |
| `wait_reached(timeout=)` | POSITION 軸は全モータの到達を並列待ち。許容差はモータごとに `MotorSpec.to_tolerance()` で換算。POSITION 以外（velocity / duty / on_off）は到達判定を持たず `settle_s` だけ待って True |
| `observed_value()` | フィードバックの逆換算（ホーミングと手動操縦が使う） |
| `sync_violation()` | `SyncGroup.violation()` に委ねる。`sync_tolerance` が無い軸は `None` |

`MotorHandle`（1 モータ）は緊急停止インターロックと `target_sink`（M3508 の位置制御ループへの迂回）を持つ。

### 3 本のシーケンス

| ファイル | 走らせ方 | 中身 |
|---|---|---|
| `sequences/main_hand.py` / `sub_hand.py` | 操縦者の `sequence_start`（それぞれのタブ） | ワークの取得 → 搬送 → 配置 / 受け取り → 吸着 → 配置 |
| `sequences/motor_check.py` | Monitor の設定面から `motor_check_start`（零点確定だけなら `homing_start`） | **両ハンド 1 本**。零点確定 → 各軸を運用で使う位置名へ動かす → `restore_home` |

**`sequences/*.py` に数値を書かない。** 共通化してよいのは「どの軸をどの位置名へ動かすか」の
**組**だけで、複数軸の組が複数ステップに現れるときだけモジュール定数（`main_hand.HOME`）か
ヘルパ関数（`main_hand._pick_at()` / `sub_hand._all_valves()`）にまとめ、単一軸の組
（`{"gripper": "closed"}` 等）はリテラルのまま残す。合成は `dict` の `|` で行う。

`require_trigger` の付与基準は「失敗したときに取り返しがつかないか」—— main_hand
「ハンド閉じる (ワーク把持)」（位置ずれのまま閉じるとワークと機構の双方を破損する）/ sub_hand
「ハンド閉じる (受け取り)」（メインハンドと機構同士が向かい合う唯一の動作）/ 両ハンドの
「ハンド開く」（落とすとやり直せない）。

### アクチュエータ動作確認（`sequences/motor_check.py`）

Monitor の設定面（`MatchPrep`）から起動する両ハンド 1 本のシーケンス。

| 要素 | 中身 |
|---|---|
| 判定 | シーケンスエンジンがそのまま担う（`tolerance` で到達判定、`duty` / `on_off` は `settle_s` の固定待ち） |
| 駆動量 | **確認専用の値を持たない**。運用で使う位置名へ動かす |
| ペア軸 | `move_to` は軸名しか受け付けないので、左右へ同時に指令が飛ぶ（除外しない） |
| ゲート | 環境側の条件（フェーズ・緊急停止・各ロボットの制御権）はサーバーが `environment_deny` として渡し、可否の判定は `MotorCheckController.deny_reason()` にしかない |
| 排他 | 全ロボットに掛かる（どちらかが手動モード / シーケンス実行中なら拒否）。**周期タスクは 1 つも止めない**（`RobotServer._motor_check_pausables` は空を返す） |
| 構成に無い軸 | `Sequence.restrict_to_axes()` が除外し、`excluded_steps`（欠けている軸まで）を同じ 1 通に載せる |
| 中断 | `MotorCheckController._abort_requested`（シーケンスの**外側**のフラグ） |
| 配信 | 進捗も結果も拒否理由も `motor_check_state` 1 通で運ぶ |

現状の各ステップと判定可否は [`checks_and_health.md`](checks_and_health.md) の
「② 統合動作確認 — 動くか」が正。

### 零点合わせだけを走らせる（`lib/server_homing.py`）

動作確認は零点確定の後に全軸を駆動する通し実行なので、零点確定だけを見たいときに
その先の失敗を巻き込む。`homing_start` はその 1 歩目だけを走らせる入口である。

| 要素 | 中身 |
|---|---|
| 宛先 | **`robot` が必須**。`axes` を省くとそのロボットの `homing:` を持つ全軸。**全ロボットを回す形は持たない**（「サブハンドのつもりでメインハンドが動く」を作らない） |
| 実行 | `run_homing()`。動作確認と同じ 1 本を通す（手順を書き写さない） |
| 失敗 | 1 軸落ちても残りを続け、軸ごとの理由を `results` に載せる（1 回で全軸の可否が分かる） |
| ゲート | `HomingController.deny_reason()`。環境側は `RobotServer._homing_environment_deny()` が渡す（動作確認と同じ `_environment_deny` を見る） |
| 排他 | 動作確認・作動点測定と相互排他。どれかが走っている間、再励磁・手動切替・試合開始も `RobotServer._busy_label()` 経由で塞がる |
| 配信 | 進捗も結果も拒否理由も `homing_state` 1 通。拒否は加えて `command_rejected` で要求元へ返す |

### リミットスイッチの作動点を測る（`lib/server_switch_measure.py`）

機構が変わるたびにスイッチの作動点は動く。**位置定数 yaml に書く値を実機から取る**入口で、
零点合わせと**同じ二段探索を通し、原点を書き込む段だけを行わない**。

| 要素 | 中身 |
|---|---|
| 宛先 | **`robot` / `axis` / `direction` が必須**。零点合わせと同じく全機を回す形は持たない |
| 向き | 指定させる。`homing.direction` を使い回さない（**測りたいのは `homing` が使わない側の端でもある**） |
| 実行 | `HomingRunner.measure()`。`home()` と同じ `_approach()`（離脱 → 粗探索 → 寄せ直し）を通り、`_capture_origin()` を呼ばない |
| 刻み・上限 | 既定は `homing` の `coarse_step` / `step` / `search_distance`。指定は `HomingSpec` を `replace()` して載せるので、**探索の各段が見る歯止めがそのまま測定の歯止めになる**（別変数で持たない）。`HomingSpec` の検証もそのまま効く |
| 結果 | 作動点・離脱点・ON 区間の幅・**使った刻み**（作動点のばらつきは刻みそのものなので、値と一緒でないと精度が読めない） |
| ゲート | `SwitchMeasureController.deny_reason()`。動作確認・零点合わせと相互排他（`RobotServer._axis_holders()` が単一情報源） |
| 配信 | 進捗も結果も拒否理由も `switch_measure_state` 1 通。拒否は加えて `command_rejected` で要求元へ返す |

対象の軸は零点合わせと共通（`HomingSource`）。`RobotServer.set_homing_source()` が両方へ配る。

### 手動操縦（`lib/manual.py`）

シーケンスからの退避路。`OperationMode`（`sequence` / `manual`）は**ロボットごとに独立**し、
正はサーバーが持つ。

| コマンド | 対象 |
|---|---|
| `set_operation_mode` | 制御権の切替。手動へ入るとき `_stop_sequence(discard_pending_start=True)` を通す。動作確認の実行中は拒否 |
| `manual_move` | 位置名で指令する。**全軸で使える**（既定義の点しか送らない） |
| `manual_set` | 人間の単位の絶対値。`axes.<軸>.manual` を持つ軸のみ |
| `manual_jog` | 直前の**手動目標**からの相対移動。同上 |

3 つの入口は `RobotServer._manual_target` で軸を解決してからモードを見る。`sequence` モードの
まま通せるのは `axes.<軸>.manual_always` を宣言した軸だけで、判定は `_allow_manual_in_sequence`
（一覧は `ManualController.always_manual_axes()` → `PositionTable.manual_always_axes()`）。この経路は
動作確認の実行中を拒否する。

指令経路はシーケンスと同一（同じ `MotorGroup` を共有し `AxisHandle` を通すので、緊急停止
インターロック・M3508 の PID 迂回・20Hz 再送・左右ペアの偏差監視がそのまま効く。ただし
`move_to` 完了時の段は手動では通らないので、効くのは常駐の段だけ —— `y_axis` は 200Hz と
50Hz、`rotate` は 50Hz のみである）。
**モータ単位の指令口を作らない**（UI にもモータ単位のジョグを出さない）。ジョグの起点は直前の
手動目標値で、起点が無い（初回・緊急停止後）ときだけフィードバックから逆換算する。モータの
目標値はモード切替で消さない（消すのはジョグの起点だけ）。範囲外の値は**拒否ではなくクランプ**
する（`ManualSpec.clamp`）。`match_reset` は全ロボットを `sequence` へ戻す。

制御権の奪い合いを両方向で塞ぐゲート（`CommandSpec.blocked_during_manual` /
`_manual_target` の判定）は §8 と [invariants.md](invariants.md) の「制御権の奪い合いは
両方向を塞ぐ」。

### 吸着パッドの選択（`lib/suction.py`）

サブハンドの吸着パッド 6 個（`valve_1`〜`valve_6`）のうち「次の吸着で開ける弁」を操縦者が
減らせる。ワークの形で乗らないパッドの弁を開けると真空が抜けるため。

| 要素 | 中身 |
|---|---|
| 正 | `SuctionSelection`（パッドの並びと有効集合）。**サーバーの `RobotContext.suction` が持つ**（操縦 UI 2 台で食い違わせない）。`sequences/sub_hand.py` が既定（全部 ON）を作り、`main.py` が `suction_of(seq)` で同じ 1 個をサーバーへ渡す |
| 変更 | `suction_pads_set`（`robot` + 使う弁の**全集合** `pads`）。差分ではなく全集合を送るので、2 台が別々に押しても最後に届いた形が正になる。知らない軸名は拒否（`command_rejected`）。**空も通す**（選び直しの途中で 0 個を経由できる） |
| 配信 | `state.suction`（`pads[]` に `axis` / `label` / `enabled`）。持たないロボットは `null`。ラベルはサーバーが付ける（UI に弁の名前を書き写さない） |
| 効き方 | 「ワーク吸着」ステップが `enabled()` を読み、**選ばれた弁だけ `open`・残りは `closed`** を 1 回の `move_to` で送る。選択が空なら `SuctionSelectionError` でそのステップを失敗させる（吸わずに進むと落とす）。実行中の吸着には反映しない（次の吸着ステップから） |
| ゲート | 無し（`PHASES_ANY`・緊急停止中も手動中も通す）。選択は機体を動かさない |
| 既存との違い | `AlwaysManualPanel` の弁操作は「今すぐ開閉」、こちらは「次の吸着で使うか」。役割が違うので両方残す |

---

## 6. 設定ファイルの構成

読み込みと検証は `lib/config_schema.py`（`load_system_config` / `load_robot_config` /
`load_position_table` / `load_checklist_definitions`）に一本化し、`main.py` は
`_load_all_configs` で呼ぶだけ。

### 分担

| ファイル | 持つもの |
|---|---|
| `config/system.yaml` | PC 上に 1 つしか存在しない設定。バス別名・`health`・`match` |
| `config/can_buses.yaml` | CAN バス定義（serial ↔ 固定名・bitrate・txqueuelen・restart_ms）の単一情報源 |
| `config/<robot>.yaml` | そのロボットのモータ構成（`robot_name` / `motors` / `sensors`） |
| `config/<robot>_positions.yaml` | 論理軸の単位換算・機構位置の定数・手動操縦の可動範囲・`motion` / `homing` / `sync_*` |
| `config/checklist.yaml` | セッティングタイムの指差喚呼チェックリスト |
| `config/bench/<対象>/` | 机上ベンチ用の一式（8 セット） |

パスは `--system` / `--config`（複数可）/ `--checklist` で差し替えられる。
位置定数の読み先は「robot config と同じディレクトリの `<robot_name>_positions.yaml`」
（`_positions_path`）。

`config/system.yaml`:

```yaml
can_buses:   # バス別名 -> SocketCAN インタフェース名
  m3508_bus: can_m3508 / edulite_bus: can_edulite
  generic_bus: can_generic / dm3520_bus: can_dm3520   # ← 実際は 1 行ずつ書く
health:                        # 4 値は HealthThresholds として必ず 1 組で運ぶ
  feedback_timeout_ms: 500     # この時間フィードバックが無ければ STALE
  temp_warning_c: 65           # WARNING
  temp_critical_c: 80          # FAULT
  tx_error_threshold: 96       # CAN error_passive 境界
match:
  duration_s: 180              # 試合時間。duration_s <= 0 は起動時に弾く
```

### `config/<robot>.yaml` のスキーマ

最上位に書けるのは `robot_name` / `motors` / `sensors` のみ。モータ共通キーは
`driver` / `bus` / `can_id` で、ドライバ固有キーは**そのドライバにしか書けない**（混在は起動拒否）。

| driver | 固有キー | 既定値 |
|---|---|---|
| `m3508` | `pid`（`kp` / `ki` / `kd` / `integral_limit` / `dead_band` / `output_limit`） | §4 の表 |
| `edulite05` | `host_id` / `mode` / `limit_speed` / `limit_current` / `position_kp` / `set_zero_on_start` | `0xFD` / `position` / 2.0 / 5.0 / 30.0 / `false` |
| `dm3520` | `master_id` / `mode` / `p_max` / `v_max` / `t_max` / `limit_speed` / `set_zero_on_start` | 実機レジスタと一致させる（§4） |
| `generic` | `control_type` / `expected_firmware` / `expected_angle_range_deg` | `position` |

正は `lib/config_schema.py` の `_DRIVER_MOTOR_KEYS` と `CAN_ID_RANGES`。`can_id` の範囲は
`generic` が `0x01`〜`0xFE`（仕様書 §2.2。`0x00` は駆動拒否、`0xFF` はブロードキャスト予約）、
`dm3520`（ESC_ID）が `0x01`〜`0x0F`（フィードバックに載るのは下位 4bit だけ）。

`motors` は yaml の記述順で保持され、空のロボットは拒否する。`motors.*.bus` は
`system.yaml` の `can_buses` に定義済みの別名だけを受け付ける。センサは `motors:` ではなく
`sensors:` セクションへ書く。

### `axes` スキーマ（`config/<robot>_positions.yaml`）

```yaml
axes:                      # 換算: command = value * scale + offset
  sub_y_axis:              # 単一モータ軸（軸名 = モータ名。scale / offset を軸直下に書く）
    unit: mm               # チームが positions に書く単位
    command_unit: rad      # モータへ実際に送る単位
    scale: 1.0668451
    offset: 0.0            # 機械原点と電気原点のずれ（指令単位）
    timeout_s: 4.0         # 到達待ちの上限。未指定なら 5.0
    tolerance: 1.0         # 到達許容差（人間の単位）。未指定ならドライバ既定値

  y_axis:                  # 複数モータで駆動する論理軸。軸名はモータ名でなくてよい
    unit: mm
    command_unit: deg
    timeout_s: 4.0
    tolerance: 1.0
    sync_tolerance: 10.0   # 左右のずれ許容（人間の単位）。超過で停止（§4 の偏差監視）
    sync_kp: 16.0          # 同期補正のゲイン
    sync_limit: 1250       # sync_kp とセットで必須
    motion: { max_velocity: 200.0, max_acceleration: 1200.0, velocity_ff: 1.0 }
                           # 台形プロファイル（§4）。velocity_ff は pid.kd と同値に保つ
    manual: { min: 0.0, max: 650.0, steps: [1.0, 10.0, 100.0] }
                           # 手動で連続値を送ってよい軸だけが書く
    homing: { sensor: …, direction: -1, step: 1.0, settle_s: 0.05, search_distance: 180.0 }
                           # 零点確定（§4）。search_distance は省略できない
    guard:                 # 可動端インターロック（§4）。書かない項目は「その守りが無い」
      limits:              # 1 本なら文字列、同じ端に複数本あるなら並びで書く
        minus: [y_axis_r_origin_sensor, y_axis_l_origin_sensor]
      # max_step / stall_torque は実測が入るまで書かない
    motors:                # scale / offset はモータごとに書く
      y_axis_r: { scale: 864.15, offset: 0.0 }
      y_axis_l: { scale: -864.15, offset: 0.0 }   # 逆回転は scale の符号で表す

  conveyor:                # 位置以外を指令する軸
    unit: duty
    command_unit: duty
    command_mode: duty     # position（既定）/ velocity / duty / on_off
    settle_s: 0.3          # 到達判定を持たない軸の指令後固定待ち [s]
    manual_always: true    # sequence モードのままでも manual_move を受け付ける（duty / on_off のみ）

positions:                 # 値は axes.<軸>.unit の単位で書く
  y_axis: { home: 0.0, work_1: 120.0, work_shared: 650.0 }
  conveyor:
    run: { red: 0.3, blue: -0.3 }   # コートで変わる位置だけ辞書で書く（両方必須）
  gripper: { open: …, closed: … }   # 離散状態アクチュエータは「名前付き状態」として書く
```

**読み込みを拒否する組み合わせ**:

| 記述 | 理由の所在 |
|---|---|
| `positions` に `axes` 未定義の軸 | 換算係数が無いまま人間の単位の値を生の指令値として送ることになる |
| `motors:` と軸直下の `scale` / `offset` の併記 | どちらが効くか曖昧 |
| モータ 1 台の軸に `sync_tolerance` | 防護が効いていないことに気付けない |
| `sync_kp` があって `sync_limit` が無い / `motion` の 2 値の片方だけ | 押し合いの歯止めが無い / 軌道が決まらない（[invariants.md](invariants.md)「保護は止めるだけ…」） |
| `command_mode: position` 以外の軸に `manual:` / `motion:` / `homing:` | 可動範囲・軌道・原点という概念が無い |
| `duty` / `on_off` 以外の軸に `manual_always: true` | シーケンスの到達待ちを手動が上書きできてしまう |
| `positions` の値が `manual` の範囲外 | 「シーケンスで行ける位置へ手動では行けない」軸ができる |
| `homing` のセンサが `guard.limits` の逆側にある／載っていない（`guard.limits` を書いた軸のみ） | 守りが反転して押されている端へ進む指令だけが通る／探索で当てた端を誰も守らない |
| `timeout_s` が `motion` の所要時間に足りない | 必ずタイムアウトする軸になる |

`main.py` 側は**起動自体は続行**する（`_load_position_table_file`）。yaml が無い／壊れて
いれば警告・エラーログを出して空の定数表を bind し、シーケンスが値を引いた時点で
`PositionLookupError` を出す。`PositionTable` が公開するのは `move_to` が使う `axis()` /
`commands()` / `sync_tolerance()`、配線が使う `paired_axes()` / `axes`、config の不変条件
テストが使う `names()` / `raw()` だけで、`AxisSpec` 側では**先頭モータしか見ない API はペア軸で
`ValueError` を投げる**（`scale` / `offset` / `to_command` / `command_tolerance`）。

`on_off` を通す許可表は 4 箇所にあり、1 つでも漏れると別々の壊れ方をする:

| 場所 | 漏れたときに起きること |
|---|---|
| `lib/drivers/base.py` の `ControlMode` | そもそも値が存在しない |
| `lib/drivers/generic.py` の `_MODE_MAP` / `_TARGET_SCALE` | 起動はできるが最初の指令で `KeyError` |
| `lib/config_schema.py` の `_CONTROL_MODES` | robot yaml に書いた瞬間に起動拒否 |
| `lib/sequence/positions.py` の `_COMMAND_MODES` | 位置定数 yaml に書いた瞬間に起動拒否 |

### `config/checklist.yaml`

```yaml
checklists:
  pre_match:
    - { id: estop_release, label: 非常停止スイッチ解除確認, group: preflight }
    - { id: court,         label: コート設定 (赤/青) と実配置の一致確認, group: court }
    - { id: motor_check,   label: アクチュエータ動作確認 完了, group: motor_check }
    - { id: field_clear,   label: 可動範囲内に人・物がないこと確認, group: final }
```

- ロールは `pre_match` 1 つだけ（= `lib/match_state.py` の `ALL_ROLES`）。ロールが 1 つでも `checklists` は辞書のまま運ぶ
- `id` はロール内で一意。`id` / `label` を欠くエントリは無視して起動する
- `group`（`preflight` / `court` / `motor_check` / `final`）は**その項目を画面のどの
  コントロールの隣に置くか**の宣言。省略可で、サーバーは語彙を検証しない。未指定・未知の
  group は UI が「その他」としてまとめて描く。対応表は `web/src/lib/checklistGroups.ts` だけ
- ファイルが無ければ項目ゼロで起動する。項目ゼロのロールは「完了」とみなす

### 机上ベンチ用の config セット（`config/bench/`）

**開くバスが違うので 1 つにまとめられない**（挿していない CANable が 1 本でもあると
`[Errno 19] No such device` で起動が失敗する）ため、対象ごとにサブディレクトリを分ける。
1 ディレクトリに同じ `robot_name` のセットは 2 つ置けない。

| セット | 対象 | 開くバス | `robot_name` | 特徴 |
|---|---|---|---|---|
| `m3508/` | M3508 2 台 | `can_m3508` | `main_hand` | `output_limit` 1000 / `sync_tolerance` 10.0mm |
| `edulite/` | EDULITE 05 2 台 | `can_edulite` | `main_hand` | `limit_speed` / `limit_current` を半分、`sync_tolerance` 15.0deg、`timeout_s` 5.0s。`homing` は書かない |
| `dm3520/` | Damiao DM3520 2 台 | `can_dm3520` | `sub_hand` | `limit_speed` を本番の半分（1.0rad/s） |
| `dc/` | 自作モタドラ DC 基板 1 枚 | `can_generic` | `main_hand` | 3ch とも `command_mode: duty`。`manual:` は書けない |
| `servo/` | 自作モタドラ サーボ基板 1 枚 | `can_generic` | `main_hand` | 4 スロット + センサ 1。**本番と違い `manual:` を書く**（角度を連続で振るため） |
| `solenoid/` | 自作モタドラ 電磁弁基板 1 枚 | `can_generic` | `main_hand` | 6ch とも `on_off` / `open`・`closed`。`manual:` は書けない |
| `main_hand/` | **サブハンド不在でメインハンド実機を動かす構成** | `can_m3508` + `can_edulite` + `can_generic` | `main_hand` | **robot yaml / positions を持たず本番の `config/main_hand.yaml` / `config/main_hand_positions.yaml` をそのまま使う。CANable 3 本が要る** |
| `y_axis_tuning/` | `y_axis` の PID 実機チューニング用 | `can_m3508` | `main_hand` | `scripts/tune_y_axis.py` が使う |

```bash
uv run python main.py --system config/bench/<対象>/system.yaml \
    --config config/bench/<対象>/<robot_name>.yaml \
    --checklist config/bench/<対象>/checklist.yaml
```

`main_hand/` だけが CANable 3 本を要求する代わりに、単体ベンチでは一度も通らない確認を担う
—— **バス名の取り違え**（`bench_no_crosstalk`: 一方を動かしている間もう一方が 1 度も動かない
こと）/ 受信ループ 3 本 + 200Hz + 20Hz + 50Hz が同時に回った状態 / 左右直結ペアが 2 組
（`y_axis` / `rotate`）同時に監視される状態 / `can_id` が両バスで 1・2 と重なっていても
衝突しないこと。

**8 セットとも `tests/test_config_schema.py::TestShippedBenchConfigs` が守る** —— ①system /
robot / positions / checklist が揃っていて読めること ②登録したモータが**すべて**位置定数から
指令できること ③開くバスがそのセットで使うものだけであること ④**同梱のディレクトリが漏れなく
`_BENCH_DIRS` に載っていること**（`test_every_shipped_bench_dir_is_covered`）⑤本番 config を
そのまま使うセットは `_BENCH_USES_PRODUCTION_CONFIG` に宣言させること。

ベンチで緩めた値（`sync_tolerance` / `output_limit` / `limit_*`）は本番へ戻す条件をコメントで
書く。`scale` と `position_kp` は本番と同じ値を使う。

---

## 7. WebSocket プロトコル

サーバーと Web UI は WebSocket の JSON でしか繋がっていない。配信経路は
`lib/ws_hub.py` の `WsHub` 1 本、受信条件は `web/src/lib/protocol.ts` の
`parseServerMessage()` 1 箇所、契約の実体は `web/src/test/ws-contract.json`（**手書き禁止**）。

### Server → Client

| type | 配信タイミング | 中身 |
|---|---|---|
| `state` | 定期（ロボットごと） | シーケンス進行・モータ状態・センサ・ヘルス・安全機構・手動軸・吸着パッドの選択 |
| `match_state` | 接続直後 1 回 + 変化時 | コート・フェーズ・`can_start_match` / `checklists` / `timer` |
| `server_info` | **接続直後 1 回だけ** | `dev_tools` / `dry_run` / 温度しきい値 2 値 |
| `e_stop_state` | 切り替わった瞬間 + 接続直後（停止中なら） | `active` と（内部検知なら）`reason` |
| `health_change` | ヘルスが変化した瞬間 | `robot` / `target` / 遷移。**`robot` は UI の受信条件が依存する** |
| `motor_check_state` | 動作確認の進捗・結果・拒否理由 | 4 種に分けず**この 1 通で運ぶ** |
| `homing_state` | 零点合わせ単独実行の進捗・結果・拒否理由 | 宛先（`robot` / `axes`）と軸ごとの成否（`results`）、ロボットごとの対象軸（`targets`） |
| `switch_measure_state` | 作動点測定の進捗・結果・拒否理由 | 宛先（`robot` / `axis` / `direction`）と実測（`result`: 作動点・離脱点・ON 区間・刻み）、ロボットごとの対象軸（`targets`） |
| `command_rejected` | 拒否時、**要求元 1 台にだけ** | `command` / `reason` |

`motor_check_start` の拒否だけは `motor_check_error` に載せる（UI の表示経路が別のため）。

#### `state` の構造

```jsonc
{
  "type": "state",
  "robot": "main_hand",
  "sequence": "pick_and_place",
  "current_step": "extend_arm",
  "step_index": 1,
  "total_steps": 5,
  "waiting_trigger": true,
  "running": true,   // シーケンス側の実行フラグそのまま（UI に step_index から推測させない）
  "steps": [{ "index": 0, "label": "初期位置へ移動", "require_trigger": false }, …],
  "motors": {   // PID ゲインは配信しない（実行中に差し替える経路が無い）
    "y_axis_r": { "pos": 1500, "vel": 0.0, "torque": 0.2, "temp": 35.0,
                  "command": 220.0, "command_mode": "position" },
    // 測る手段が無い項目は null（DC 基板は 4 値とも、サーボ基板は位置以外）。command は
    // PC が最後に送った指令値であって実出力ではない（未指令なら null）
    "conveyor":  { "pos": null, "vel": null, "torque": null, "temp": null,
                   "command": 0.3, "command_mode": "duty" }
  },
  // 自作基板のセンサ入力。motors とは別に運ぶ（config の sensors: と同じ分け方）。active は
  // 接触しているか（接触は異常ではない。報告手段が無いドライバでは null）、stale の境界は
  // health.feedback_timeout_ms が唯一の正
  "sensors": { "rotate_origin_sensor": { "active": true, "stale": false } },
  "e_stop_active": false,
  "health": { /* HealthSnapshot。overall は判定失敗時も down（ok に倒さない） */ },
  "safety": {
    "sync_violations": ["y_axis"],       // ラッチ中の軸名（ループと SyncMonitor の和集合）
    "loops_running": true, "monitors_running": true, "refreshers_running": true,
    "reenergizing": false,
    "firmware_unconfirmed_motors": [],   // INFO 未受信で焼き忘れ検出が働いていないモータ
    "position_loops":   [{ "bus": "m3508_bus", "running": true, "paused": false,
                           "sync_violations": ["y_axis"] }],
    "sync_monitors":    [{ "axes": ["y_axis", "rotate"], "running": true, "violated": [] }],
    "target_refreshers":[{ "motors": ["conveyor", "gripper"], "running": true, "paused": false }]
  },
  "manual": {
    "mode": "sequence",                  // "sequence" | "manual"
    "axes": [{
      "name": "y_axis", "unit": "mm", "command_mode": "position",
      "value": 12.3,                     // フィードバックの逆換算。位置を測れない軸は null
      "target": 12.0,                    // 直前の手動目標。一度も送っていなければ null
      "manual": { "min": 0.0, "max": 650.0, "steps": [1.0, 10.0, 100.0] },  // 不可なら null
      "manual_always": false,            // sequence モードのままでも manual_move を受け付ける軸か
      "deviation": 0.32,                 // SyncGroup.deviation() をそのまま配る。0.0 は正常値
      "sync_tolerance": 10.0,            // UI にフォールバック値を持たせないため一緒に配る
      "positions": ["home", "work_1", "work_shared"],
      "motors": ["y_axis_r", "y_axis_l"]
    }]
  },
  "suction": {                           // 吸着パッドを持たないロボットは null
    "pads": [{ "axis": "valve_1", "label": "1", "enabled": true }]
  }
}
```

`steps` / `manual.axes` は静的だが `state` に載せる（**UI にモータ名も軸名も可動範囲も書かせ
ない**ため）。`motors` と `steps` は素通しで、数値を実際に読む側が `readMeasured()` を通す。
`motor_check_state` の `steps` だけは例外で構造（`index` / `label` / `require_trigger`）を検査する。

#### `e_stop_state` / `match_state` / `server_info`

```jsonc
{ "type": "e_stop_state", "active": true }
{ "type": "e_stop_state", "active": true,
  "reason": "main_hand の y_axis の左右ずれ 3.400mm が 許容 2.000mm を超えました" }
```

`reason` は操縦者操作の `e_stop` では付かない。サーバーが保持し、解除まで再配信のたびに
載せる。最初に判明した理由を優先し、後から来た理由で上書きしない。

```jsonc
{ "type": "server_info", "dev_tools": false, "dry_run": false,
  "temp_warning_c": 65.0, "temp_critical_c": 80.0 }

{
  "type": "match_state",
  "court": "red",
  "phase": "setup",
  "can_start_match": false,
  "checklists": {
    // checklists のキーがそのまま試合開始のゲート対象ロール
    "pre_match": { "items": [{ "id": "court", "label": "コート設定 …",
                               "checked": false, "group": "court" }],
                   "completed": false }
  },
  // 残り時間ではなく「この配信瞬間の経過ミリ秒」を配る
  "timer": { "running": false, "elapsed_ms": 0, "duration_ms": 180000 }
}
```

### Client → Server

```jsonc
// シーケンス制御（4 つとも "robot" を取る）
{ "type": "sequence_start" | "sequence_stop" | "trigger", "robot": "main_hand" }
{ "type": "sequence_jump", "robot": "main_hand", "step_index": 3 }
// 安全
{ "type": "e_stop" }   { "type": "e_stop_release" }
{ "type": "reenergize_motors", "robot": "main_hand" }
// 手動操縦
{ "type": "set_operation_mode", "robot": "main_hand", "mode": "manual" }  // "sequence" | "manual"
{ "type": "manual_move", "robot": "main_hand", "axis": "gripper", "position": "open" }
{ "type": "manual_set",  "robot": "main_hand", "axis": "y_axis", "value": 12.5 }  // 人間の単位
{ "type": "manual_jog",  "robot": "main_hand", "axis": "y_axis", "delta": -0.5 }
// 試合運用
{ "type": "set_court", "court": "blue" }
{ "type": "checklist_set", "role": "pre_match", "item_id": "court", "checked": true }
{ "type": "checklist_reset", "role": "pre_match" }     // role 省略で全ロール。UI からは送らない
{ "type": "checklist_check_all", "role": "pre_match" } // 開発用。--dev-tools 起動時のみ受理
{ "type": "match_start" | "match_finish" | "match_reset" }
// 動作確認・状態
{ "type": "motor_check_start" | "motor_check_abort" | "health_check" }
// 零点合わせだけを走らせる。robot は必須（axes 省略でそのロボットの homing: を持つ全軸）
{ "type": "homing_start", "robot": "sub_hand", "axes": ["sub_y_axis"] }
// 次の吸着で開ける弁の全集合。差分ではない
{ "type": "suction_pads_set", "robot": "sub_hand", "pads": ["valve_1", "valve_2"] }
// リミットスイッチの作動点を測る。robot / axis / direction は必須
// step / coarse_step / limit は省略可（省くと homing の値が既定になる）
{ "type": "switch_measure_start", "robot": "sub_hand", "axis": "sub_y_axis",
  "direction": -1, "step": 0.5, "limit": 60.0 }
```

**シーケンス制御コマンドのセマンティクス**:

| コマンド | 意味 |
|---|---|
| `sequence_start` | 先頭から実行開始（停止後・完走後の再起動） |
| `sequence_stop` | 通常停止（緊急停止と異なり CAN 層には介入しない）。`step_index=0` / `running=false` へ戻る |
| `sequence_jump` | 任意のステップへ。実行中なら次のステップ境界で反映、停止中・完走後なら指定 index から再開。`step_index` は `int` だけを受け付け `bool` は弾く |
| `trigger` | `require_trigger=true` のステップを次へ進める |

**シーケンスは起動時に自動実行しない。** `match_start` はフェーズを進めるだけで機体は
動かさない。「停止したらどこへ戻るか」はシーケンス自身（`run_forever()`）の責務。

**パラメータ変更（`set_param`）と `/pid-tuning` タブは存在しない。** PID ゲインを実行中に
差し替える経路は無い（CAN の `SET_PARAM` フレームは別物で、仕様書が持つ）。

### 契約テスト

| ファイル | 役割 |
|---|---|
| `tests/test_ws_contract.py` | 実物の `RobotServer` を aiohttp のテストサーバーで起動し、WS へ配信させたメッセージを型ごとに 1 通ずつ捕まえて golden と突き合わせる |
| `web/src/test/ws-contract.json` | 生成物。**手書き禁止** |
| `web/src/test/wsContract.test.ts` | 同じ JSON を import し、`useRobotSocket` の**受信経路へ流し込んで**状態が更新されることを検証する |

```bash
UPDATE_WS_CONTRACT=1 uv run pytest tests/test_ws_contract.py   # 再生成
```

**片側だけが増えた状態を検出する仕掛けが 3 つ** —— `test_contract_covers_every_broadcast_type`
（Python。配信し得る型が golden に欠けていないことを `REQUIRED_TYPES` で固定）/「契約の全
サンプルに TS 側の検証がある」（TS。`samples` と `EXPECTATIONS` のキー集合の一致）/「state
サンプルに UI が読むフィールドが揃っている」（TS。`running` / `safety` 等の存在）。

フィクスチャには**測れる項目が違うドライバを必ず両方載せる**（サーボ基板 = 位置だけ /
DC 基板 = 1 つも測れない）。変動値は `$placeholders`（`epoch_seconds` / `duration_ms`）で
固定値へ差し替えるが、`null` は潰さない。

---

## 8. 試合運用フロー

`lib/match_state.py` が試合全体の状態を一元管理する。ロボット単位の `state` とは別系統で、
**正は必ずサーバー側**にある。

### 3 つの直交する軸

| 軸 | 値 | 範囲 | 意味 |
|---|---|---|---|
| `court` | `red` / `blue` | 全体で 1 つ | 自陣コート。赤青で配置が左右反転する |
| `phase` | `setup` → `ready` → `match` → `finished` | 全体で 1 つ | セッティングタイムと試合中を分離 |
| `mode` | `sequence` / `manual` | **ロボットごと** | 制御権を誰が握っているか |

`mode` だけがロボット単位なのは、メインハンドを調整しながらサブハンドの試合進行を続ける
運用が成立しなければならないため。**全自動モードは存在しない。**

### フェーズ遷移

```
setup ⇄ ready → match → finished → setup
  ↑ 必要チェックリスト完了で自動遷移 (ready)、チェックが外れると setup に戻る
              ↑ match_start (明示操作のみ)
                      ↑ match_finish   ↑ match_reset (どのフェーズからでも可)
```

ゲート対象ロールは **`pre_match` 1 つだけ**（= `ALL_ROLES`）。`court` を変更すると
チェックリストは**全リセット**され `setup` に戻る。`match_reset` はコートを維持したまま
チェックリストのみリセットし、全ロボットを `sequence` へ戻す。未知のコート値は理由付きで
拒否する（`command_rejected` に有効値を添える）。**試合を開始できるかを決めるのはサーバーの
`can_start_match` だけ。**

フェーズ集合は `PHASES_ANY`（全フェーズ = 素通りさせるという宣言）/ `PHASES_OUTSIDE_MATCH`
（`setup` / `ready` / `finished`）/ `PHASES_PREPARATION`（`setup` / `ready`）/
`PHASES_START_GATE`（`ready`）/ `PHASES_DURING_MATCH`（`match`）。UI 側の写しは `web/src/lib/phase.ts` の `isDuringMatch()` **だけ**（`PHASES_OUTSIDE_MATCH` は
その補集合）。レイアウトの出し分けに使う `isSetupPhase()` とは別物。

### コマンド語彙の単一情報源（`lib/commands.py`）

1 コマンド = 1 `CommandSpec`。名前・許可フェーズ・フェーズ拒否理由・緊急停止中の可否と
その拒否理由・ハンドラ名・拒否通知経路を**同じ 1 行**に置く。`CommandSpec` は
どのフィールドにも既定値を持たず、`__post_init__` が「ゲートするのに理由文が無い」
「全フェーズ許可なのに拒否理由が書いてある」といった矛盾を import 時に弾く。

`lib/server.py` は語彙を持たない。`handle_command` は `spec_for(type)` で仕様を引き、
3 段のゲート（開発用 → フェーズ → 緊急停止）を掛け、`spec.handler` の名前で `_cmd_*` を呼ぶ。
**語彙に無いコマンドは拒否理由も返さず黙って捨てる。**

#### フェーズによるコマンドゲート

| コマンド | setup | ready | match | finished | 許可フェーズ集合 |
|---|:-:|:-:|:-:|:-:|---|
| `set_court` | ✓ | ✓ | ✗ | ✓ | `PHASES_OUTSIDE_MATCH` |
| `motor_check_start` / `homing_start` / `switch_measure_start` | ✓ | ✓ | ✗ | ✓ | `PHASES_OUTSIDE_MATCH` |
| `checklist_set` / `checklist_reset` / `checklist_check_all` | ✓ | ✓ | ✗ | ✗ | `PHASES_PREPARATION` |
| `match_start` | ✗ | ✓ | ✗ | ✗ | `PHASES_START_GATE` |
| `match_finish` | ✗ | ✗ | ✓ | ✗ | `PHASES_DURING_MATCH` |
| `sequence_start` / `sequence_jump` / `trigger` | ✗ | ✗ | ✓ | ✗ | `PHASES_DURING_MATCH` |
| `sequence_stop` / `e_stop` / `e_stop_release` / `match_reset` | ✓ | ✓ | ✓ | ✓ | `PHASES_ANY` |
| `motor_check_abort` / `health_check` / `reenergize_motors` | ✓ | ✓ | ✓ | ✓ | `PHASES_ANY` |
| `set_operation_mode` / `manual_move` / `manual_set` / `manual_jog` | ✓ | ✓ | ✓ | ✓ | `PHASES_ANY` |
| `suction_pads_set` | ✓ | ✓ | ✓ | ✓ | `PHASES_ANY` |

最後の 4 行は「書き忘れ」ではなく**無ゲートであることの宣言**である。

#### 緊急停止によるコマンドゲート（二段目・独立）

| コマンド | 緊急停止中 | 理由 |
|---|:-:|---|
| `sequence_start` / `sequence_jump` / `trigger` / `match_start` | ✗ | シーケンスが進むと次のステップが停止指令を上書きする（拒否文はコマンドごとに別。`lib/commands.py`） |
| `motor_check_start` | ✗ | 同上（拒否は `motor_check_error` で通知） |
| `homing_start` / `switch_measure_start` | ✗ | 同上（拒否は `command_rejected` で通知） |
| `manual_move` / `manual_set` / `manual_jog` | ✗ | 目標値を送るため |
| `set_operation_mode` | ✓ | 機体を動かさない切替そのもの |
| `sequence_stop` / `e_stop` / `e_stop_release` / `motor_check_abort` | ✓ | 止める方向の操作は緊急停止中こそ通す |
| `match_reset` / `match_finish` / `health_check` | ✓ | 復帰経路と状態確認は塞がない |
| `set_court` / `checklist_*` | ✓ | 機体を動かさず、復旧には指差喚呼のやり直しが要る |
| `suction_pads_set` | ✓ | 選択を変えるだけで、効くのは次の吸着ステップ |

#### その他のゲート

| フィールド | 対象コマンド | 意味 |
|---|---|---|
| `blocked_during_manual` | `sequence_start` / `sequence_jump` / `trigger` | そのロボットの制御権を実際に奪う 3 つだけ（`sequence_stop` は退避の逃げ道として通す） |
| `blocked_during_reenergize` | 同上 | 再励磁中に `move_to` が書いた目標を上書きさせない |
| `requires_dev_tools` | `checklist_check_all` | `--dev-tools` / `CBC_DEV_TOOLS=1` 起動時のみ受理。UI 側は `serverInfo.dev_tools` で描画を分ける 2 重ゲート |
| `reject_channel` | 全コマンド | 拒否をどの経路で返すか（既定 `command_rejected`、`motor_check_start` だけ `motor_check_error`） |

拒否は**全配信しない**（要求元 1 台への返答であって全員への通知ではない）。要求元が居ない
経路（HTTP POST・内部の安全機構からの発動）では誰にも送らない。`motor_check` の HTTP POST
経路は `handle_command` を通らないため、`_start_motor_check` 側にも同じフェーズ判定を置く。

### 試合時間タイマー（全デバイス同期）

**単調時計アンカー方式**。サーバーは時刻ではなく**その配信瞬間の経過ミリ秒**
（`timer.elapsed_ms`）だけを配り、各デバイスはそれを起点に自分の単調時計で進める。

| 層 | 時刻源 |
|---|---|
| サーバー（`MatchState`） | `time.monotonic()` |
| クライアント（`MatchTimer.tsx`） | `performance.now()` |

リロード・途中接続は接続直後に送る `match.to_dict()` スナップショットがそのままアンカーになり、
`_broadcast_match_state()` が走るたびに再アンカーされる。秒表示の更新は固定間隔ではなく
**秒境界に合わせて**起こす。状態遷移はフェーズ遷移が成立した**後**にだけ動かし、凍結の解除は
`match_reset` だけが行う。**0 到達は表示が止まるだけ**で、試合終了はあくまで操縦者の
`match_finish`。試合時間は `config/system.yaml` の `match.duration_s`（既定 180 秒）。

### 開発用コマンド

`--dev-tools`（CLI）または `CBC_DEV_TOOLS=1`（環境変数。systemd unit 等から渡す用）で
起動したときだけ、指差喚呼の一括チェック（`checklist_check_all`）を受理する。既定は無効。
フラグの正はサーバーが持ち、接続直後の `server_info` で UI へ配る。一括チェックは
`logger.warning` で必ずログに残す。

配った `dev_tools` には**サーバーの語彙を増やさない UI 側の効果**もあり、緊急停止オーバーレイ
（`EStopOverlay`）を操縦者が隠せるようになる。隠しているあいだは全幅の警告帯（`EStopBanner`）が
代わりに出て解除ボタンを載せる。新しい WS コマンドは無く、UI は `serverInfo.dev_tools` を
**描画のたびに**見る。理由は [`invariants.md`](invariants.md) §8、画面は
[`web/screens.md`](web/screens.md)。

---

## 9. Web UI の構成

**画面・部品カタログ・配色・データフロー・踏みやすい罠は [`web/`](web/) の 4 枚が持つ。**
ここに置くのは、リポジトリ全体から見た位置づけだけである。

| | |
|---|---|
| スタック | Vite + React + TypeScript + Tailwind v4 / daisyUI 5（テーマ `cbc`）。パッケージマネージャは pnpm@10 |
| 配信 | 制御プログラムと同一プロセス。`lib/server.py` が `web/dist/` を SPA 配信する |
| タブ | URL パス（`/monitor` `/main-hand` `/sub-hand`）。操縦者 2 名 + Monitor の 3 画面 |
| 状態 | WS 受信 → `lib/protocol.ts`（型と受信条件）→ `lib/robotReducer.ts`（純関数）→ context 3 分割 |
| 判定の置き場所 | ヘルスは `lib/healthVerdict.ts`、動作確認の完了は `lib/motorCheckStatus.ts`、フェーズは `lib/phase.ts`。いずれも 1 箇所だけ |

| 読みたいこと | 文書 |
|---|---|
| どの画面に何が出るか・部品カタログ | [`web/screens.md`](web/screens.md) |
| 配色・ラベル・アイコン・確認の取り方・EMG STOP の配置 | [`web/design.md`](web/design.md) |
| WS 契約・受信境界・context 分割・接続先の解決 | [`web/data_flow.md`](web/data_flow.md) |
| 踏んだ罠と、それを守っているテスト | [`web/pitfalls.md`](web/pitfalls.md) |
| コマンドとディレクトリ | [`../web/README.md`](../web/README.md) |

崩してはならない UI の不変条件は [`invariants.md`](invariants.md) §8。


## 10. サービス運用（systemd）

### 3 つの unit

| unit | 中身 | enable |
|---|---|---|
| `cbc-can.service` | `setup_can.sh --wait 15`（CAN バス up）。`Type=oneshot` + `RemainAfterExit=yes` | **する**（電源投入で up） |
| `cbc-can-watchdog.service` | `can_watchdog.sh`（bus-off 復旧の常駐）。`Requires=cbc-can.service` | **する**（機体を動かさない） |
| `cbc-control.service` | `.venv/bin/python -u main.py`。`Wants=cbc-can.service` + `After=` | **しない**（手動 `systemctl start`） |

配置・enable・撤去の 3 つは `install.sh` の 1 つの配列（`UNITS` / `AUTOSTART_UNITS`）が
まとめて回す。

配置・起動・ログ追跡のコマンドは [`operations.md`](operations.md)。

`cbc-control.service` の性質:

| 設定 | 中身 |
|---|---|
| `ExecStartPre` | `/usr/bin/test -f` で `web/dist` の存在を確認する（ビルドそのものは持たない） |
| `ExecStart` | `.venv/bin/python` を**絶対パス**で指す（mise の shim は systemd の PATH に無い） |
| `Restart` / `StartLimitBurst` / `RestartSec` | `on-failure` / 3 / 2 —— **約 6 秒で `failed` に固定**され、復帰に `reset-failed` が要る |
| 依存 | `Wants=cbc-can.service`（`Requires=` にしない）。`After=` の順序宣言はそのまま |
| 停止 | SIGTERM を `_install_stop_signal_handler()` が cancel へ変換し、後始末経路へ合流させる |

`cbc-can.service` は `--strict` を付けずに呼ぶので **CAN が 0 本でも success で終わる**。
「揃っているか」に答えるのは人で、導線は指差喚呼の `can_bus_strict`。

### シェルスクリプト

| ファイル | 役割 |
|---|---|
| `scripts/_common.sh` | 4 本のシェルが source する土台（`SCRIPT_DIR` / `PYTHON` / `CAN_CONFIG` の存在確認 / EUID による sudo の有無 / `log_*` / 引数解析の作法）。**どこへもコピーされない** |
| `scripts/can_config.py` | `can_buses.yaml` → TSV / udev ルール / 固定パス変換。udev ルールのパスと service 名は `can_config.py paths` が答える |
| `scripts/setup_can.sh` / `can_watchdog.sh` | CAN バス up（冪等・`--strict` / `--wait`）/ bus-off で送信停止したバスを down/up で復旧させる常駐 |
| `scripts/install.sh` / `deploy.sh` | udev / systemd への配置と有効化（`--uninstall`）/ 依存導入 + Web UI ビルド + 再起動（`--no-install`） |
| `scripts/edulite_set_id.py` / `tune_y_axis.py` | EDULITE 05 の CAN ID 走査・書き換え・照合 / `y_axis` の PID 実機チューニング CLI（`uv run` 専用） |

**ログの接頭辞だけは統一していない** —— journal では `[ OK ]` / `[ WD ]` / `[install]` /
`[deploy]` で発生元の unit を見分けるので、各スクリプトが `LOG_PREFIX` を上書きする。

### bus-off 復旧ウォッチドッグ

`can_watchdog.sh` は **qdisc の backlog が残っていること**と **TX packets が進んで
いないこと**の **AND** で判定し、down/up で復旧させる。復旧の down/up の途中で殺されると
バスが down のまま残るので、後始末は EXIT トラップが持つ。判定を `ip link` の `can state` や
エラーフレームに置けない理由と、復旧が電磁弁のウォッチドッグと噛み合う件は
[`checks_and_health.md`](checks_and_health.md) と
[invariants.md](invariants.md) の「bus-off からの復旧はウォッチドッグが持つ」。

---

## 11. テスト戦略

プロトコル層とシーケンスエンジンは TDD で開発する（RED → GREEN）。実機デバッグで時間が
溶けやすいバイト列の組み立てミスと状態遷移のバグを、テストで先に潰す。

実行コマンドは [`operations.md`](operations.md)。

### テスト対象とアプローチ

| レイヤー | TDD | 手法 |
|---|:-:|---|
| 4 種のドライバのプロトコル（PC 側） | ◎ | エンコード / デコードの単体テスト。期待するバイト列との比較。DM3520 はパラメータ応答の除外も |
| 自作モタドラプロトコル（ファーム側） | ◎ | `pio test -e native`。プロトコル層・緊急停止ラッチ・ウォッチドッグ・角度補間・電磁弁の出力ゲート |
| シーケンスエンジン / モータアクセス層 | ◎ | ドライバを mock し、ステップ遷移・trigger 待ち・到達待ち・緊急停止拒否を検証 |
| PID / 位置制御ループ / 周期タスク | ◎ | 時刻・sleep・CAN 送信を注入して差し替え、実時間を待たずに周期を駆動 |
| 緊急停止・フェーズ・制御権のゲート | ◎ | WS 経由でコマンド拒否・シーケンス停止・解除後の復帰を結合テスト |
| 機構位置定数 / config パース | ◎ | yaml → 換算後の指令値、コート差異、欠損・記述ミス時の挙動、同梱 config の読み込み |
| ロボット固有シーケンス | ◎ | 「どの軸にどの値を送ったか」を検証。値は試験用の定数表で与える |
| CAN 受信ループの堅牢性 | ◎ | 解釈できないフレーム・落ちるドライバ・`bus.recv` の失敗・`rx_down` の立ち下がり |
| CAN バス命名（udev ルール生成） | ◎ | `scripts/can_config.py` の出力書式を固定（`setup_can.sh` が TSV の列に直接依存する） |
| WS メッセージ契約 | ◎ | golden JSON を両側が見る（§7） |
| Web UI (React) | ○ | vitest + jsdom + Testing Library |
| aiohttp サーバー | △ | `aiohttp.test_utils` で最低限の結合テスト |
| **CAN 実通信（vcan）** | ✗ | **未着手。** `tests/test_can_manager.py` は `can.Bus` をモックしており SocketCAN の層は通っていない |

### テストヘルパ

`tests/` は対象モジュールと 1:1 に近い名前で並ぶ（`tests/drivers/` に 4 ドライバ + 横断の
`test_target_reached` / `test_driver_contract`、制御層・シーケンス・CAN・config・配線・
`test_server_*`・WS 契約）。ヘルパは 6 つ —— `server_fixtures.py`（サーバーの組み立て・駆動・
WS 待ち合わせ）/ `fake_can.py`（CAN 層のモックと受信状態の作成）/ `feedback_frames.py`
（実機と同じフレームの組み立て）/ `fake_drivers.py` / `fake_health.py` / `fake_clock.py`。

**内部へ手を伸ばす特権は `tests/server_fixtures.py` と `tests/fake_can.py` の 2 ファイルだけ**が
持ち、その理由を各ファイル冒頭に書く（テスト本体は公開 API で書く）。
`tests/feedback_frames.py` は特権を持たない 3 つ目のヘルパで、実機と同じフレームを
`update_state` へ流し込む唯一の場所である。

### ファームウェアの native テスト

テストは `firmware/test/`（`test_protocol` / `test_board` / `test_servo` / `test_solenoid`）に
あり、`dc_motor` / `servo` の両プロジェクトが `test_dir = ../test` で同じものを指す。
**どちらから回しても同じ全ケースが走る。** 各スイートが何を検証しているかの一覧は
`firmware/README.md` の「テストの方針」が持つ。`main.cpp` / `app.cpp` はペリフェラル依存の
ため対象外（宛先判定・周期管理・シリアル行組み立て・安全機構との結線はすべて `MotorCan` 側へ
出してある）。**送信バッファまわりは native テストで守れない層**なので、触ったら実機に
書き込んで `candump` で確かめる。

### vcan を使った統合テスト（未着手）

SocketCAN のフレーム往復は実機の 4 本でしか通っていない。埋めるなら
`sudo modprobe vcan && sudo ip link add dev vcan0 type vcan && sudo ip link set up vcan0` を
前提に、`CANManager` を実バス相手に走らせる結合テストを足すことになる。

### 変異テスト

**テストを足した／張り替えたら、本番コードにわざと 1 行の不具合を入れ、狙ったテストが
落ちることを確認してから元に戻す**（変異の前後で `__pycache__` を捨てる）。多重防護は
層ごとに、他の層を持たない条件を作って 1 枚ずつ確かめる。手順・変異の例・落ちるべき
テストの対応表は [invariants.md](invariants.md) の §9 テスト。

---

## 12. 未解決の課題

実装済みだが実機・運用面で未対応の項目。**安全機構の「今どうなっているか」は
[`checks_and_health.md`](checks_and_health.md)**、実測値の棚卸しは
[`mechanism_handoff.md`](mechanism_handoff.md) §0 が正である。

### 安全系

| 課題 | 現状 |
|---|---|
| `_e_stop_active` がプロセスメモリ上のみ | サーバーを再起動すると緊急停止状態が消える。物理的な緊急停止ボタンの状態と同期する仕組みも無い |
| 緊急停止で fault がラッチされた場合の復帰手順が無い | `e_stop_release` は `activate_motors()` を呼ぶが `encode_disable(clear_fault=True)` は送らない（fault の自動クリアは原因を隠すため意図的に行っていない）。実機で「解除しても動かない」場合は `health` の `FAULT` 表示で fault の内容を確認して電源再投入 |
| フィードバックが得られないモータが無励磁のまま残る | `activate_motor()` は待機（既定 0.5s）中にフィードバックを受け取れないと enable を送らず WARNING をログに出すだけ。有効化を見送ったモータを UI に出す仕組みが欲しい |
| ホーミングは `rotate` だけ実機検証済み | `search_distance` はまだ効く経路を通っていない。`step` 1.0deg での通し確認も未取得。`y_axis` はスイッチ未装着で `homing:` ごとコメントアウト中 |
| 零点確定が有効なのは `rotate` だけ | `sub_y_axis` / `sub_lift`（DM3520）はドライバが `supports_origin_capture()` を宣言しない（`SET_ZERO` の安全な順序が `disable` を要求し、`sub_lift` は自重で落ちる）。`y_axis` は手段があるがスイッチ未装着。原点が確定できない軸は電源投入位置がそのまま原点で、ずれは指差喚呼が人の目で埋める |
| down したバスでも起動できてしまう | 起動ログへ 1 行 ERROR を残すが起動は拒否しない（`--strict` を通していない構成を一律に潰さない判断）。受信ループは `rx_down` を立てて `BusHealth.DOWN` を出す |
| `config/bench/main_hand/checklist.yaml` が `can_generic` 側の確認項目を持たない | この構成では本番の `gripper` / `conveyor` / `wall_*` / `rotate_origin_sensor` が構成に入るが、`conveyor_run` / `origin_sensor_react` に相当する項目が無い。`bench_return_home` の文言も `y_axis` の実態とねじれている |
| `config/bench/y_axis_tuning/system.yaml` のコメントが本番の `sync_tolerance` と食い違う | ベンチ 2.0mm に対し本番 10.0mm。どちらが正かは現時点の記述からは判断できない |

### 制御・チューニング

| 課題 | 現状 |
|---|---|
| 機構定数は軸によって実測済みと仮値が混在する | **実測済み**: メインハンド `y_axis` の `pid` / `motion` / `sync_kp` / `positions` / `manual`、`rotate` の `positions` / `manual` / 原点スイッチの極性 / `homing.direction`。**仮値**: サーボ 3 軸（`gripper` / `wall_f` / `wall_r`）の `positions` とファームの可動域、`conveyor.run` のコート別の符号、`rotate` の `homing.search_distance`、`y_axis` の `homing`、**サブハンドはほぼ全部** |
| M3508 の位置制御の一部が実機未検証 | 多回転アンラップ・到達判定とも単体テストのみ。PID は 150mm で取り直し済みだが、**短距離（15mm）での `sync_kp` の取り直しが残る**（最適値が振幅で変わる軸である） |
| 低速域のスティックスリップが未観測 | 予測であって観測ではない。対抗手段は `ki`（既に 10）と `velocity_ff`。静摩擦補償は今回スコープ外 |
| PID ゲインと `velocity_ff` は実行中に変更できず UI にも配信されない | 調整は config 変更 + 再起動。`pid.kd` と `motion.velocity_ff` は 2 つの yaml にまたがる対 |
| **200Hz が実機で維持できるかの実測データが無い** | `PeriodicTask` が実周期を測って journal へ集計 1 行を出すところまでは入っているが、**画面と WS 配信からは意図的に外してある**（しきい値が実機未検証のため）。次にやるべきは本番の CAN 構成での計測 |
| `dead_band=1.0`（モータ軸 deg）と `default_tolerance` の関係 | 現状は許容差（出力軸 1deg ≒ モータ軸 19.2deg）> デッドバンドなので到達するが、両者を動かすときは大小関係を意識する（デッドバンドが広いと永久に到達しない） |

### 運用

| 課題 | 現状 |
|---|---|
| 位置定数の実機反映手順が手動 | メインハンドの `y_axis` / `rotate` は反映済み。`gripper` / `wall_*` / `conveyor` とサブハンド側は未着手 |
| vcan を使った統合テストが 1 つも無い | §11 |
| CANable が 1 本欠けると起動できず、systemd で起動不能に固定される | 約 6 秒で `failed`。復旧に `reset-failed` が要る。片ハンドだけで出る逃げ道はある（`can_edulite` / `can_generic` は両ハンドが使うので逃げられない）。切り分けは [`venue_recovery.md`](venue_recovery.md) §1 |
| `cbc-can.service` は CAN が 0 本でも success で終わる | **意図的**（`--strict` を付けると片ハンド練習・ベンチ・`--dry-run` で毎回 `failed` が残り、`Requires=` の `cbc-can-watchdog.service` が上がらなくなる）。揃っているかは指差喚呼 `can_bus_strict` が人に確認させる |
| `deploy.sh` が会場でネットワークを要求しうる | `--no-install` を足した。**会場入りの前に一度ネットワークのある場所で素の `deploy.sh` を回してキャッシュを温めておく** |
| CAN 復旧の down/up が基板のウォッチドッグを満了させる | 電磁弁が消磁して吸着中のワークが落ちる。試合中かどうかのゲートは意図的に置いていない。`rx_down_episodes` と `may_affect_workpiece` を配信し、`SubsystemStatus` がチップで主張する。手順は [`venue_recovery.md`](venue_recovery.md) §3-1 |

---

## 13. 参考リンク

### 本リポジトリの文書

| 文書 | 役割 |
|---|---|
| [`invariants.md`](invariants.md) | 崩してはならない設計と、その理由・失敗様式 |
| [`checks_and_health.md`](checks_and_health.md) | 点検とヘルスの全体像（ヘルス監視 / 動作確認 / 常駐保護 / 指差喚呼の 4 系統）。**「今どうなっているか」はこれ** |
| [`venue_recovery.md`](venue_recovery.md) / [`mechanism_handoff.md`](mechanism_handoff.md) | 会場カード（試合当日に手が止まったときはこれ 1 枚）/ 機構が付いた日に埋める値の棚卸し |
| [`motor_driver_can_protocol.md`](motor_driver_can_protocol.md) | 自作モータドライバ CAN プロトコルの単一情報源 |
| [`history/`](history/) / `firmware/README.md` | 実装の経緯・日誌 / 通電前の要確認項目・デバイス ID・安全既定値・native テストの方針 |

### RobStride EDULITE 05

RobStride シリーズは全モデル共通プロトコル。**CAN 2.0B Extended Frame（29bit ID）/ 1Mbps**、
ID 構造は `[通信タイプ 5bit][データエリア2 16bit][宛先ID 8bit]`、デフォルト モータ ID は `0x7F`
（**バスへ載せる前に 1 台ずつ書き換える**）、制御モードは MIT(0) / 位置(1) / 速度(2) / 電流(3)、
フィードバックは角度・角速度・トルク・温度（各 16bit → 物理量に線形マッピング）。

- 公式 GitHub: https://github.com/RobStride ／ EDULITE A3（Python SDK + ROS2）: https://github.com/RobStride/EDULITE_A3
- STM32 サンプル: https://github.com/RobStride/SampleProgram ／ Rust crate: https://docs.rs/robstride/latest/robstride/
- Seeed Studio Wiki: https://wiki.seeedstudio.com/robstride_control/

### Damiao DM3520

情報源は `DM-S3519-1EC User Manual`（`/home/drc/repos/dm3520_test` に PDF と ESP32 の
サンプルコード、ESC_ID 走査用の `dm_s3519_scan_can_id` 環境がある）。
