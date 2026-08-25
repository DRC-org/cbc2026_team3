# CBC2026 Team3 中央制御プログラム 実装計画

## 概要

キャチロボバトルコンテスト 2026 に出場するロボットの中央制御プログラム。
固定型ロボットにメインハンドとサブハンドがあり、それぞれ半自動シーケンス制御で動作する。

### 技術スタック

- **バックエンド**: Python 3.12+ / asyncio（単一プロセス）
- **CAN 通信**: python-can + SocketCAN
- **Web UI**: Vite + React + TypeScript（画面切替はタブ + URL ハッシュ。ルーターは使わない）
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
- **Python メイン**: シーケンス制御の記述性を優先。制御ループの大半はモータ側で閉じており、中央 PC からは目標値送信のみで済む。**ただし M3508 だけは例外で、位置ループを PC 側で持つ**（下表）
- **aiohttp 採用**: 静的ファイル配信と WebSocket を 1 プロセスで統合でき、localhost:8080 で全機能を提供可能

#### PID がどこで閉じているか

| モータ | 位置ループの所在 | PC が送るもの |
|---|---|---|
| RobStride EDULITE 05 | **モータ内蔵ドライバ**。起動時に `run_mode=位置` と `PARAM_LOC_KP`（既定 30.0、`config` の `position_kp`）を書き込み、実測角を保持目標に書いてから励磁する（「Phase 9: 励磁の有効化」参照） | 目標角のみ（`PARAM_LOC_REF` への float 書き込み） |
| 自作モタドラ（DC / サーボ） | **モタドラのマイコン**。制御タイプは `SET_MODE` / `SET_PARAM` で設定 | 目標値のみ（`SET_TARGET` フレーム） |
| DJI M3508 (C620) | **電流ループのみ ESC 内。位置ループは PC 側**（`lib/control/position_loop.py`、既定 200Hz） | 電流指令（0x200 フレーム、4 モータ分を 1 通に束ねる） |

C620 は電流指令しか受け付けない（`M3508Driver.encode_target` は CURRENT 以外を `ValueError`）ため、
リフト軸の位置決めには PC 側で `累積角 [deg] → 電流 [counts]` の外側ループを回すしかない。
内側の電流ループは ESC 内で高速に閉じているので、カスケード構成の外側だけを 200Hz で回す形になる。

#### asyncio で 200Hz の位置ループを回す判断

**成立する理由**:

- 1 周期の仕事は「フィードバック鮮度の確認 → PID 演算 → 0x200 フレーム 1 通の送信」だけで、演算量は無視できる
- 制御対象は外側の位置ループのみ。内側の電流ループは C620 側で閉じているので、
  数 ms のジッタが即座にトルクリップルになる性質のループではない
- `dt` は固定値を仮定せず毎周期 `time.monotonic()` の実測差分を使う。
  周期が揺れても PID の積分・微分は時間的に正しく計算される

**限界（隠さずに書く）**:

- **周期は保証されない。** ループは `await asyncio.sleep(0.005)` を処理の後に置く実装なので、
  実効周期は「処理時間 + 5ms + イベントループの遅延」であり厳密な 200Hz ではない。
  GC の停止、WebSocket ブロードキャスト、ヘルスチェック、シーケンスの同期処理など
  同一イベントループ上の別タスクが長く CPU を握れば、その分だけ制御周期がまるごと飛ぶ
- **CAN 送受信は `run_in_executor`（既定スレッドプール）越し**（`lib/can_manager.py`）。
  送信 200Hz/バス と受信ポーリング 100Hz/バス（`_RECV_TIMEOUT = 0.01`）が同じプールを共有するため、
  バスを増やすとスレッドプールの待ちが周期に乗る
- Python / SCHED_OTHER / GIL の上で動く以上、**ハードリアルタイム性は無い**。
  実機で周期の乱れが問題になる場合は、位置ループをマイコン側（自作モタドラ相当）へ移す設計変更が必要

**乱れたときに壊さないための備え**（詳細は「M3508 の位置制御（PC 側 PID）」の安全側の挙動表）:

| 事象 | 備え |
|---|---|
| 周期が飛んだ | PID に渡す `dt` を `DEFAULT_MAX_DT_S`（50ms = 制御周期の 10 倍）で頭打ち。実測 dt をそのまま渡して積分・微分が跳ねるのを防ぐ |
| フィードバック途絶 | `health.feedback_timeout_ms`（既定 500ms）超過で電流 0 + PID リセット。古い実測値で PID を回して暴走させない |
| 周期処理で例外 | 0 電流を送ってループは継続（ループを抜けて指令が途切れると C620 が惰走する） |
| 緊急停止 | 電流 0 + PID リセット + 目標解除 |
| 一時停止からの復帰 | `resume()` で全軸 PID リセット + `_last_tick` 取り直し（停止時間が丸ごと `dt` に化けない） |

**未検証**: 上記はいずれも単体テスト（`tests/test_position_loop.py`）でのみ確認しており、
実機で 200Hz が維持できるかは測定していない（「未解決の課題」参照）。

---

## ハードウェア構成

### モータ

| モータ | 個数 | ESC/ドライバ | CAN プロトコル |
|---|---|---|---|
| DJI M3508 | 2 | C620 ESC | CAN 2.0A Standard Frame |
| RobStride EDULITE 05 | 2 | 内蔵 | CAN 2.0B Extended Frame (29bit) |
| DC モータ / サーボ | 多数 | 自作モータドライバ | CAN 2.0A Standard Frame（後述） |

### CAN バス構成（3 系統）

| バス（固定名） | CANable | USB serial | 接続デバイス | ビットレート |
|---|---|---|---|---|
| `can_m3508` | #1 | `004600224E4D501520343332` | M3508 × 2 | 1 Mbps |
| `can_edulite` | #2 | `006F004A4E4D501820343332` | EDULITE 05 × 2 | 1 Mbps |
| `can_generic` | #3 | `0068005C4E4D501520343332` | DC モータ / サーボ（自作モタドラ） | 1 Mbps |

#### インターフェース名を固定する理由

`can0` / `can1` / `can2` の番号は USB の列挙順（挿す順・ハブのポート・起動タイミング）で
決まり、個体との対応を保証しない。番号が入れ替わると C620 に EDULITE 用のコマンドが飛び、
モータを破損しうる。

CANable2（candleLight FW）は STM32 UID 由来の USB serial を持つため、udev でシリアル一致の
固定名を割り当ててこれを回避する。定義は `config/can_buses.yaml` に集約し、udev ルールと
セットアップスクリプトの双方をそこから生成・参照する（二重管理の防止）。

#### セットアップ

```bash
sudo scripts/install.sh            # udev ルール配置 + systemd 有効化（初回のみ）
scripts/setup_can.sh               # 手動 up。見つかったバスだけ立ち上げる（開発用）
scripts/setup_can.sh --strict      # 試合前点検。3 本揃わなければ異常終了
sudo scripts/install.sh --uninstall
```

PC 起動時は `cbc-can.service`（`Type=oneshot` + `RemainAfterExit=yes`）が
`setup_can.sh --wait 15` を実行する。`--wait` は USB 列挙が起動直後に間に合わない場合の
待ち時間。USB 抜き差し時も udev の `RUN+=` により service が再実行される。

`setup_can.sh` は冪等で、up 済みのバスも一度 down してから再設定する
（`ip link set type can` は down 中しか受け付けないため）。up 後は `ERROR-ACTIVE` を
確認してから成功を返す（`ip link set up` の成功は通信可能を意味しないため）。

#### `can_buses.yaml` を編集したら install.sh の再実行が必須

udev ルールは `can_buses.yaml` から生成されるため、yaml を編集しただけでは `/etc` 側に
反映されない。反映漏れは「serial を書いたのに固定名にならない」という分かりにくい形で
現れるため、`setup_can.sh` は起動時に yaml と配置済みルールを比較し、ズレていれば警告を
出す。`--strict` では失敗として扱う（定義と実態がズレたまま試合に入るのを防ぐため）。

```
[WARN] config/can_buses.yaml と配置済み udev ルールが一致しません
[WARN]   -> sudo scripts/install.sh を再実行してください
```

#### `--wait` のデッドラインは全バスで共有する

待ち時間をバスごとに消費すると、欠けが N 本あるとき N × `--wait` 秒かかる。実測で
2 本欠け時に起動が 31 秒まで伸びたため、デッドラインはメインループ開始時に一度だけ
確定させ、全バスで共有する。全 CANable は同じ USB 列挙で現れるので待ちを分ける意味はない。

#### 新しい CANable の serial 採取手順

USB ハブ入手後、残り 2 個について以下を実行する。

```bash
# 対象の 1 個だけを挿した状態で（他が挿さっていると can0 がどれか判別できない）
udevadm info -a -p /sys/class/net/can0 | grep -m1 'ATTRS{serial}'
```

得られた値を `config/can_buses.yaml` の該当バスの `serial` に記入し、
**`sudo scripts/install.sh` を再実行する**（これを忘れると反映されない）。`TBD` のままの
バスは udev ルールに出力されず、`setup_can.sh` の対象からも外れる。

3 個とも採取済みのため、通常この手順が必要になるのは CANable を交換したときだけ。

#### 既知の制約: バス down 時の失敗が分かりにくい

実測した挙動は以下のとおり。

| インターフェースの状態 | `_create_bus()` | 受信ループ |
|---|---|---|
| 存在しない | `OSError: [Errno 19] No such device` で起動失敗 | — |
| 存在するが down | **オープン成功。例外は出ない** | `bus.recv` が `CanOperationError` を投げて即死 |
| up | 正常 | 正常 |

問題は 2 行目。`CANManager.run()` は `_receive_loop` を `asyncio.create_task` で起こす
だけで例外を回収しないため、受信タスクの死亡が握りつぶされる。結果としてモータ状態が
一切更新されず、ヘルスチェックが全モータを STALE と報告する。原因が「バスが down」だと
特定するのは難しい。

`cbc-can.service` により通常は起動時に up されるため実害は出にくいが、service が失敗した
場合などに起きうる。対策候補は次の 2 つで、いずれも今後の課題とする。

1. `_create_bus()` にインターフェースの `operstate` 検証を追加し、down なら起動を止める
2. `_receive_loop` に例外ハンドラを入れ、バス異常をヘルススナップショットへ反映する

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

### M3508 の位置制御（PC 側 PID）

M3508 は C620 ESC 経由で**電流指令しか受け付けない**（`encode_target` は CURRENT 以外を
`ValueError`）。リフト軸には位置決めが必要なため、PC 側で `位置 [deg] → 電流 [mA]` の PID を回す。

- `lib/control/pid.py` — モータ非依存の PID（測定値微分 / conditional integration / デッドバンド）
- `lib/control/position_loop.py` — `M3508PositionLoop`：**CAN バス単位**の非同期制御ループ

**バス単位でまとめる理由**: C620 の電流指令フレーム（0x200）は 1 通に 4 モータ分のスロットを持つ。
モータごとに個別送信すると自分以外のスロットを 0 で上書きしてしまい、同一バス上の他モータが
カクつく。そのため全モータ分の電流を `M3508Driver.encode_current_frame()` で 1 フレームに束ね、
`CANManager.send_to_bus()` で 1 周期 1 通だけ送る。

**多回転**: `decode_feedback()` の `position` は既存 API 互換のため 0〜360 のまま。累積角は
`M3508Driver.update_state()` でラップアラウンドをアンラップして保持し、`multi_turn_position`
（deg）で公開する。ホーミング後は `reset_multi_turn_origin()` で原点を張り直す。

**到達判定**: 目標値が累積角なので、`M3508Driver` は `_observed_for(POSITION)` を
`multi_turn_position` にオーバーライドする（基底のままだとラップ角と比較してしまい、
何回転もする軸では目標 720deg に対しラップ角 0deg を比べる／たまたま一致して誤到達する）。
フィードバックはモータ軸基準のため、`default_tolerance(POSITION)` は共通既定の 1deg を
減速比 `GEAR_RATIO`（3591/187 ≒ 19.2）倍し、他ドライバと同じ「出力軸 1deg」に揃える。

**目標値の流れ**: `MotorHandle.target_sink`（`lib/sequence/motors.py`）に
`M3508PositionLoop.target_sink(name)` を差し込む。シーケンスが `set_position()` を呼ぶと目標
累積角が更新され、実際の CAN 送信は制御ループが代行する。`ControlMode.CURRENT` はホーミングで
機構端に押し当てる用途として PID を通さず素通しし、VELOCITY / DUTY は `ValueError`。

**安全側の挙動**:

| 条件 | 挙動 |
|------|------|
| 緊急停止中（`is_estop_active`） | 電流 0 + PID リセット + 目標解除（解除だけでは動き出さない） |
| フィードバック途絶（`health.feedback_timeout_ms` 超過） | 電流 0 + PID リセット |
| 周期処理で例外 | ログを残して 0 電流を送り、ループは継続（指令断は C620 の惰走を招く） |
| 周期が飛んだ（asyncio スタール） | PID に渡す `dt` を `DEFAULT_MAX_DT_S`（50ms）で頭打ち |
| 一時停止中（`pause()`） | **1 通も送らない**（0 電流フレームすら送らない）。緊急停止は状態のみ反映 |

制御周期は既定 200Hz（`DEFAULT_INTERVAL_S = 0.005`）。`dt` は毎周期 `time.monotonic()` の
実測差分を使う（asyncio のジッタがあるため固定 dt を仮定しない）。

#### アクチュエータ動作確認との排他（0x200 の奪い合い）

アクチュエータ動作確認（`lib/motor_check.py`）は `M3508Driver.encode_target()` で
**自分のスロットだけ埋めて他を 0 にした 0x200 フレーム**を送る。一方この制御ループは
目標未設定でも安全のため 0 電流フレームを送り続ける。両者を同時に走らせると相互に
フレームを上書きし、動作確認でモータが回らず FAILED / TIMEOUT になる。

そこで**ループ側を黙らせる方向**で排他を取る（`pause()` / `resume()`）。

| 方式 | 採否 | 理由 |
|------|------|------|
| ループを `stop()` → 終了後 `start()` | ✗ | タスクの再生成に失敗すると復帰できず、リフトが保持電流を失ったまま残る |
| **一時停止フラグ（採用）** | ✓ | `resume()` は同期メソッドでフラグを戻すだけ。`finally` から確実に呼べて失敗しない |
| 対象軸の目標だけ解除 | ✗ | 目標が無くてもループは 0 電流フレームを送り続けるため排他にならない |

- `pause()` は `_step_lock` を取ってからフラグを立てるため、**戻った時点で「送信中の 1 周期」も
  完了済み**であることを保証する（await 中の周期が動作確認の指令を後から上書きするのを防ぐ）
- 一時停止中も**緊急停止の判定だけは行う**（目標解除 + PID リセット）。送信は行わない
  （緊急停止自体の 0 電流送信は `RobotServer._handle_command("e_stop")` が別経路で行う）
- `resume()` は**目標値を残したまま全軸の PID をリセット**する。動作確認でモータが動かされて
  いるため、古い積分と前回測定値を持ち越すと復帰した瞬間に大電流が出る。目標を消さないのは、
  保持していた昇降軸が復帰時に落下しないようにするため
- `resume()` は `_last_tick` も取り直す（停止していた時間が丸ごと `dt` に化けるのを防ぐ。
  `max_dt_s` の頭打ちがあるが、意味のない大 `dt` を PID に渡さない）

### main.py での配線

`main.py` が config から部品を組み立て、シーケンスに注入する。

| 関数 | 役割 |
|------|------|
| `_load_pid_config(motor_name, motor_cfg)` | `motors[name].pid` を読み `_DEFAULT_PID` で補完 |
| `_build_position_pid(motor_name, motor_cfg)` | `make_position_pid()` + 出力レンジの絞り込み |
| `_build_position_loops(...)` | M3508 が居る**バスごとに 1 つ** `M3508PositionLoop` を生成 |
| `_wire_robot_motors(...)` | `build_motor_group()` → `Sequence.bind_motors()`。生成したループを返す |

生成したループは `server.add_robot(robot_name, seq, can_manager, position_loops=loops)` で
`RobotServer` にも渡す（動作確認との 0x200 排他に使う）。

**緊急停止インターロック**: `RobotServer.e_stop_active`（読み取り専用プロパティ）を参照する
チェッカを、`build_motor_group(is_estop_active=...)` と `M3508PositionLoop(is_estop_active=...)`
の**両方**に渡す。前者はシーケンスからの指令自体を `EStopActiveError` で拒否し、後者は
既に走っている PID ループの出力を電流 0 に落とす。片方でも渡し忘れると、緊急停止中に
実行中ステップがモータを動かせてしまう。

**ライフサイクル**: `CANManager.run()` 後に `loop.start()`、`finally` で `await loop.stop()` →
`CANManager.shutdown()` の順。例外・Ctrl-C のどちらで抜けてもループを止める（止まらないと
電流指令が出続ける）。

**PID ゲインの config スキーマ**（`config/main_hand.yaml` の `motors.lift_motor.pid`）:

| キー | 既定値 | 意味 |
|------|--------|------|
| `kp` / `ki` / `kd` | 2.0 / 0.0 / 0.0 | 累積角 [deg] → 電流指令 [counts] |
| `integral_limit` | `null` | 積分項の出力寄与上限 [counts]（`null` で無制限） |
| `dead_band` | 1.0 | 偏差の不感帯 [deg] |
| `output_limit` | 2000 | 電流指令の絶対値上限 [counts]。`CURRENT_MAX` (16384) で頭打ち |

`pid` セクションが無い M3508 は既定値で動く（起動失敗にしない）。既定値は機構未完成を前提に
「暴れない」ことを優先した仮値であり、**実機で要チューニング**。`output_limit` の既定 2000
（≒2.4A、C620 フルスケールの約 12%）は、暴走しても人力で押さえられる範囲に留めるための制限。

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
| **PID / 位置制御ループ** | ◎ | 時刻・sleep・CAN 送信を注入して差し替え、実時間を待たずに周期を駆動。積分ワインドアップ、dt 頭打ち、フィードバック途絶、pause/resume を単体テスト |
| **モータアクセス層** | ◎ | `MotorHandle` / `MotorGroup` の目標送信・到達待ち・緊急停止拒否を mock ドライバで検証 |
| **緊急停止ゲート** | ◎ | WS 経由でコマンド拒否・シーケンス停止・解除後の復帰を結合テスト |
| **機構位置定数** | ◎ | yaml → 換算後の指令値、コート差異、欠損・記述ミス時の挙動を単体テスト |
| **ロボット固有シーケンス** | ◎ | モータを mock し「どの軸にどの値を送ったか」を検証。値は試験用の定数表で与えるので、実機の位置定数を変えてもテストは追随不要 |
| **config パース** | ○ | YAML → ドライバインスタンス生成の単体テスト |
| **CAN 実通信** | △ | vcan（仮想 CAN）を使った統合テスト。CI でも実行可能 |
| **WebSocket プロトコル** | ○ | JSON パース/生成の単体テスト |
| **aiohttp サーバー** | △ | aiohttp.test_utils で最低限の結合テスト |
| **Web UI (React)** | ○ | vitest + jsdom + Testing Library。WS メッセージ処理・ホットキー抑止・状態→表示の分岐を単体テスト |

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
├── drivers/
│   ├── test_m3508.py            # M3508 エンコード/デコード・多回転累積角
│   ├── test_edulite05.py        # EDULITE 05 エンコード/デコード
│   ├── test_generic.py          # 自作プロトコル エンコード/デコード
│   └── test_target_reached.py   # is_target_reached / default_tolerance（全ドライバ横断）
├── test_pid.py                  # PID 単体（ワインドアップ・デッドバンド・出力制限）
├── test_position_loop.py        # M3508PositionLoop（周期・dt 頭打ち・途絶・pause/resume）
├── test_sequence_engine.py      # シーケンスエンジンの状態遷移
├── test_sequence_court_auto.py  # コート伝播と全自動通過 / auto_stop 停止
├── test_sequence_motors.py      # MotorHandle / MotorGroup / build_motor_group
├── test_sequence_positions.py   # 位置定数の読み込み・単位換算・コート差異
├── test_sequence_move_to.py     # bind_positions / move_to / タイムアウト時の停止
├── test_robot_sequences.py      # robots/*.py の各ステップが送る指令の検証
├── test_can_manager.py          # vcan を使った統合テスト
├── test_can_manager_health.py   # 受信タイムアウト → STALE、送信失敗 → DOWN
├── test_health.py               # ヘルス判定・状態遷移・JSON シリアライズ
├── test_motor_check.py          # MotorCheckRunner（PASSED/FAILED/TIMEOUT・abort）
├── test_match_state.py          # モード / コート / フェーズ / チェックリスト
├── test_ws_protocol.py          # WebSocket JSON プロトコル
├── test_server_health.py        # WS の health 同梱・GET /health・health_change
├── test_server_match.py         # 試合運用フローとフェーズゲート
├── test_server_motor_check.py   # 動作確認の WS イベント列と競合拒否
├── test_server_e_stop.py        # 緊急停止でのシーケンス停止とコマンドゲート
├── test_main_wiring.py          # main.py の配線（PID 生成・バス単位ループ・インターロック）
├── test_main_positions_config.py    # 位置定数 yaml の読み込みと欠損時の挙動
├── test_main_health_config.py       # health セクションの集約
└── test_main_motor_check_config.py  # motor_check セクションの集約とモータ別上書き
```

共通 fixture は各テストファイル内に置いており、`tests/conftest.py` は現時点では作っていない。

### フロントエンドテスト（vitest）

テストは対象ソースの隣に `*.test.ts(x)` として置き、共通ヘルパのみ `src/test/` に集約する。

```
web/
├── vitest.config.ts             # vite.config.ts とは分離（cloudflare プラグインを読まない）
└── src/
    ├── test/
    │   ├── setup.ts             # jest-dom マッチャ登録・各テスト後の cleanup
    │   ├── mockWebSocket.ts     # サーバー側イベントを任意に発火できる WebSocket スタブ
    │   └── robotContext.tsx     # RobotProvider ラッパと既定コンテキスト値
    ├── lib/{cx,phase}.test.ts
    ├── hooks/{useRobotSocket,useHotkeys,useMotorCheck}.test.ts(x)
    └── components/{TriggerButton,Toaster,Checklist,HealthIndicator}.test.tsx
```

テスト対象の優先順位は「壊れたときに実機で困る度合い」で決めている。

- `useRobotSocket` — WS メッセージ 8 種の分岐、ヘルスイベントのリングバッファ、
  モータチェック record のマージ、切断時の再接続
- `useHotkeys` — 修飾キー・入力欄・モーダル表示中の抑止。競技中の誤爆は機体破損に直結する
- `Toaster` / `TriggerButton` / `Checklist` — 状態から表示・活性が一意に決まることの確認

`vitest.config.ts` を `vite.config.ts` と分けているのは、後者が build/preview で
cloudflare プラグイン（workerd）を有効化するため。テストに Worker ランタイムは不要。

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

cd web && pnpm test              # フロントエンド（watch）
cd web && pnpm test:run          # フロントエンド（1 回だけ実行）
cd web && pnpm check             # lint + format + 型検査 + テスト
```

---

## ディレクトリ構成

```
cbc2026_team3_central/
├── pyproject.toml
├── config/
│   ├── main_hand.yaml
│   ├── sub_hand.yaml
│   ├── can_buses.yaml      # CAN バス定義の単一情報源（serial ↔ 固定名）
│   ├── checklist.yaml      # 指差喚呼チェックリスト定義
│   ├── main_hand_positions.yaml  # メインハンドの機構位置定数（単位換算込み）
│   └── sub_hand_positions.yaml   # サブハンドの機構位置定数（単位換算込み）
├── scripts/
│   ├── can_config.py       # can_buses.yaml → TSV / udev ルール変換
│   ├── setup_can.sh        # CAN バス up（冪等・--strict / --wait 対応）
│   ├── cbc-can.service     # systemd unit テンプレート
│   └── install.sh          # udev / systemd への配置と有効化
├── lib/
│   ├── __init__.py
│   ├── can_manager.py
│   ├── match_state.py      # モード / コート / フェーズ / チェックリスト
│   ├── health.py           # ヘルス状態の列挙・スナップショット・動作確認レコード
│   ├── motor_check.py      # MotorCheckRunner（アクチュエータ動作確認）
│   ├── drivers/
│   │   ├── __init__.py
│   │   ├── base.py
│   │   ├── m3508.py
│   │   ├── edulite05.py
│   │   └── generic.py
│   ├── control/
│   │   ├── __init__.py
│   │   ├── pid.py             # モータ非依存 PID
│   │   └── position_loop.py   # M3508 のバス単位位置制御ループ
│   ├── sequence/
│   │   ├── __init__.py
│   │   ├── engine.py
│   │   ├── motors.py          # シーケンスからのモータ指令・到達待ち
│   │   └── positions.py       # 機構位置定数の読み込みと単位換算
│   └── server.py
├── robots/
│   ├── __init__.py
│   ├── main_hand.py
│   └── sub_hand.py
├── web/
│   ├── package.json
│   ├── tsconfig.json
│   ├── vite.config.ts
│   ├── vitest.config.ts
│   ├── index.html
│   └── src/
│       ├── main.tsx
│       ├── App.tsx                 # タブ + URL ハッシュで画面切替（ルーターは使わない）
│       ├── index.css
│       ├── context/
│       │   └── RobotContext.tsx
│       ├── hooks/
│       │   ├── useRobotSocket.ts
│       │   ├── useHotkeys.ts
│       │   └── useMotorCheck.ts
│       ├── lib/
│       │   ├── cx.ts
│       │   ├── phase.ts            # isSetupPhase / isMatchPhase
│       │   ├── robots.ts
│       │   └── tuiColor.ts
│       ├── pages/
│       │   ├── Dashboard.tsx
│       │   ├── RobotControl.tsx
│       │   └── MotorTuning.tsx
│       ├── test/                   # vitest 共通ヘルパ（setup / mockWebSocket / robotContext）
│       └── components/
│           ├── AppHeader.tsx        # フェーズ / MODE / COURT / EMG STOP + タブ
│           ├── ConnectionBanner.tsx # WS 切断の全幅バナー
│           ├── Toaster.tsx          # 操作拒否・ヘルス異常の通知
│           ├── SequenceProgress.tsx
│           ├── SequenceStepList.tsx
│           ├── CurrentStepPanel.tsx
│           ├── TriggerButton.tsx
│           ├── MotorStatus.tsx
│           ├── MotorSummary.tsx
│           ├── MotorCheckButton.tsx
│           ├── MotorCheckPanel.tsx
│           ├── HealthIndicator.tsx
│           ├── RobotReadiness.tsx   # Monitor 用の 1 行サマリ
│           ├── Checklist.tsx        # 指差喚呼チェックリスト
│           ├── MatchControl.tsx     # モード/コート切替 + 試合開始・終了
│           └── EStopOverlay.tsx     # 全画面フラッシュ + ツイスト解除
├── main.py
└── tests/                          # 構成は「テスト戦略 > テストファイル構成」を参照
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

緊急停止状態は定期配信の `state` に載るほか、切り替わった瞬間に専用イベントを push する
（`state` の配信周期を待つと EMG STOP の表示が遅れるため）。WS 接続直後にも、
緊急停止中であれば 1 回送る:

```jsonc
{ "type": "e_stop_state", "active": true }
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

### 緊急停止によるコマンドゲート

フェーズゲートとは独立した二段目のゲート。フェーズが `match` のままでも緊急停止中は
以下のコマンドを拒否する（`lib/server.py` の `_E_STOP_DENY_MESSAGES`）。緊急停止中に
シーケンスが進むと、次のステップが新しいモータ目標値を送って停止指令を上書きしてしまう。

| コマンド | 緊急停止中 | 拒否理由 |
|---|:-:|---|
| `sequence_start` | ✗ | 緊急停止中のためシーケンスを開始できません |
| `sequence_jump` | ✗ | 緊急停止中のためステップ移動できません |
| `trigger` | ✗ | 緊急停止中のためトリガーを送れません |
| `match_start` | ✗ | 緊急停止中のため試合を開始できません |
| `set_param` | ✗ | 緊急停止中のためパラメータを変更できません |
| `motor_check_start` | ✗ | 緊急停止中のため動作確認を実行できません（`motor_check_error` で通知） |
| `sequence_stop` / `e_stop` / `e_stop_release` / `match_reset` / `match_finish` | ✓ | 止める方向・復帰方向の操作は緊急停止中こそ通す |

拒否通知はフェーズゲートと同じ `command_rejected` イベント（`motor_check_start` のみ
`motor_check_error`）。UI 側でボタンを無効化するだけでは WS 直叩き・リロード直後を防げない。

`match_start` を載せる理由: 緊急停止は `match_reset` → チェックリスト再実施で `ready` に
戻れるため、フェーズゲートだけでは素通りする。全自動では `match_start` が両ロボットの
`request_start()` を兼ねるので、素通りすると緊急停止中に試合開始ボタンだけで両機が動き出す。
ゲートは `_handle_match_start()` より手前に置き、フェーズ遷移そのものを起こさない
（緊急停止中に試合フェーズへ入れること自体が異常なため安全側に倒す）。

`set_param` を載せる理由: 現状はログ出力のみだが、実装が入ると緊急停止中にモータの
制御パラメータを書き換えられてしまう（停止状態の前提が崩れる）。

### 試合開始の挙動（モード別）

- **半自動**: `match_start` はフェーズを `match` にするだけ。各操縦者が自分のタブで `sequence_start` を押す
  （ハンドごとに開始タイミングが異なるため）
- **全自動**: 操縦者タブが無いので `match_start` が両ロボットの `request_start()` を兼ねる

### 全自動モードの実現方式

シーケンス定義は半自動と共用し、`Sequence.set_auto_advance(True)` で `require_trigger` のステップを
待たずに通過させる。二重メンテを避け、半自動と全自動で挙動が乖離しないようにするため。

人間の目視確認が必須な危険動作には `@step("...", require_trigger=True, auto_stop=True)` を付ける。
`auto_stop=True` のステップは全自動でも必ずトリガー待ちで停止する。

**`auto_stop` の付与基準（機構未確定の現状）**: 「失敗したときに機構が壊れるか」で線を引く。
壊れるものだけに付ける。得点を落とすだけの失敗にまで付けると全自動が半自動と変わらなくなり、
モードを分けた意味が無くなるため。

| ステップ | require_trigger | auto_stop | 理由 |
|---|---|---|---|
| main_hand「ハンド閉じる (ワーク把持)」 | ✓ | ✓ | 位置ずれのまま閉じるとワークと機構の双方を破損する |
| sub_hand「ハンド閉じる (受け取り)」 | ✓ | ✓ | メインハンドと機構同士が向かい合う唯一の動作。衝突で両方壊れる |
| main_hand「ハンド開く (リリース)」 | ✓ | — | 落とすとやり直せないので半自動では配置位置到達を目視確認させる。ただし破損はしないので全自動は止めない |
| sub_hand「ハンド開く (配置)」 | ✓ | — | 同上 |

機構が固まって位置決め精度が確認できたら、把持の `auto_stop` を外して全自動を通しで回す。

### コート対応

`Sequence.court` で参照できる。`move_to` は現在のコートを `PositionTable` に自動で渡すため、
コートで値が変わる位置は `config/<robot_name>_positions.yaml` 側で
`{ red: <値>, blue: <値> }` と書くだけでよい（「機構位置定数」セクション参照）。
現状の軸構成（昇降・関節・グリッパ）には左右反転する軸が無いため、同梱 yaml はすべてスカラー。

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

| タブ | キー | ページ | 内容 |
|---|---|---|---|
| Monitor | `1` | `pages/Dashboard.tsx` | 試合制御 (`MatchControl`)、指差喚呼、両ロボット監視 |
| Main Hand | `2` | `pages/RobotControl.tsx` | 準備中は指差喚呼＋動作確認、試合中はシーケンス操作 |
| Sub Hand | `3` | `pages/RobotControl.tsx` | 同上 |
| PID Tuning | `4` | `pages/MotorTuning.tsx` | モータ個別調整 |

### フェーズ連動レイアウト

Monitor / RobotControl は `phase` でレイアウトごと切り替える（`lib/phase.ts` の
`isSetupPhase` / `isMatchPhase`）。準備中に試合用の操作系を並べても押せず、
試合中に設定 UI を並べても使わないため、その時に使うものだけを画面に出す。

| | setup / ready | match / finished |
|---|---|---|
| Monitor | `MatchControl`(full) + 指差喚呼 + `RobotReadiness`（異常有無のみの 1 行サマリ） | `MatchControl`(compact: 試合終了/リセットのみ) + 両機の詳細カード |
| RobotControl | `Checklist` + `SEQUENCE PREVIEW`（参照専用）+ 診断カラム | `CurrentStepPanel` + STEP LIST + 診断カラム + START/STOP/NEXT |

`MatchControl` の compact variant は必須。試合中に `MatchControl` を全て隠すと
`match_finish` の導線が消え、試合を終われなくなる（`match_finish` は MATCH フェーズ限定）。

### キーボード操作（`hooks/useHotkeys.ts`）

- `1`–`4`: タブ切替。表示中のタブは URL ハッシュ（`#main-hand` 等）に載せ、リロードで復帰する
- `Space`: 表示中のロボットの NEXT / START。非アクティブな `TabPanel` は unmount されるため、
  ハンドラは表示中のロボットにだけ効く
- 修飾キー併用・キーリピート・入力欄フォーカス中・モーダル表示中（`.tui-modal.active`）は一切発火しない

### その他の UI 方針

- ヘッダー帯 `AppHeader` にフェーズ（地色）/ MODE / COURT と EMG STOP を常時表示
- WS 切断時は `ConnectionBanner` を画面上端に全幅表示（値が更新されていないことを明示）
- 通知は `Toaster` に一本化（操作拒否 + ヘルス異常、右下に最大 3 件スタック）
- 試合中以外は START / NEXT / ステップジャンプを UI 上でも無効化する（サーバー側ゲートとの二重防御）
- 通常停止（STOP）は確認ダイアログを挟まない。安全側の動作であり、止めるまでの時間を延ばさない
- WS 接続先はページ origin から導出する（`hooks/useRobotSocket.ts`）。別 PC・タブレットからの
  アクセスに対応するため。vite dev では `/ws` を 8080 へプロキシする（`vite.config.ts`）
- `@cloudflare/vite-plugin` は build / preview のみで有効にする（`vite.config.ts`）。dev サーバーは
  制御 PC 上のローカル UI 開発専用で Worker ランタイムを必要とせず、miniflare 起動に伴う
  `Request.cf` 取得（外部通信）と起動遅延を避けるため

---

## config.yaml 構造

```yaml
robot_name: main_hand

can_buses:                 # 値は udev で固定した名前（can0/can1/can2 は使わない）
  m3508_bus: can_m3508
  edulite_bus: can_edulite
  generic_bus: can_generic

motors:
  lift_motor:
    driver: m3508
    bus: m3508_bus
    can_id: 1
    pid:                   # PC 側位置制御 PID（M3508 のみ。省略時は既定値）
      kp: 2.0
      dead_band: 1.0
      output_limit: 2000
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

## 機構位置定数（`config/<robot_name>_positions.yaml`）

**シーケンス本体に生の数値を書かない。** 3 種類のモータで指令の単位がばらばら
（M3508 = モータ軸 deg / EDULITE 05 = rad / 自作モタドラ = deg）なため、
シーケンスに数値を直書きすると単位事故が起きる。目標値と単位換算は
`config/<robot_name>_positions.yaml` に一元化し、`lib/sequence/positions.py` が読む。

機構が未完成の間は仮値（安全側の小さい可動量）を置いておき、
**機構完成後はこの yaml の数値だけを差し替える**。`robots/*.py` は触らない。

```yaml
axes:                      # 人間の単位 → モータ指令値の換算: command = value * scale + offset
  lift_motor:
    unit: mm               # チームが positions に書く単位
    command_unit: deg      # モータへ実際に送る単位（M3508 はモータ軸 deg）
    scale: 864.15          # 360 * (3591/187) / リード[mm/rev]
    offset: 0.0            # 機械原点と電気原点のずれ（指令単位）
    timeout_s: 4.0         # 到達待ちの上限。未指定なら 5.0s
    # tolerance: 0.5       # 到達許容差（人間の単位）。未指定ならドライバ既定値
  arm_joint:
    unit: deg
    command_unit: rad
    scale: 0.017453292519943295   # deg → rad

positions:                 # 値は axes.<軸>.unit の単位で書く
  lift_motor:
    home: 0.0
    work_3: 10.0
    place: { red: 10.0, blue: -10.0 }   # コートで変わる位置だけ辞書で書く（両方必須）
```

**設計上の決定事項**:
- `positions` に `axes` 未定義の軸が出てきたら**読み込みを拒否**する。換算係数が無いまま
  人間の単位の値を生の指令値として送ると機構を壊すため、checklist.yaml のような
  「壊れていても起動する」方針は取らない。
- ただし main.py 側は**起動自体は続行**する（`_load_position_table_file`）。
  yaml が無い／壊れている場合は警告・エラーログを出して**空の定数表**を bind し、
  シーケンスが値を引いた時点で `PositionLookupError` を出す。
  定数が無くてもアクチュエータ動作確認とヘルス監視は実施したいため。
- コート差異は**位置の値だけ**を `{red:, blue:}` に切り替えられる形にした。
  機構未確定の段階で反転テーブルのような抽象化を作り込むと外れたときの手戻りが大きく、
  「必要になった位置だけスカラーを辞書に書き換える」方式なら追加コストがほぼ無いため。
  現状の同梱 yaml はすべてスカラー（メイン／サブとも昇降・関節・グリッパのみで、
  左右反転する軸を持たない）。

## シーケンス記述例

```python
from lib.sequence.engine import Sequence, step

class PickAndPlace(Sequence):
    @step("初期位置へ移動")
    async def move_to_home(self):
        # {軸名: 位置名} を渡すと、換算・指令・到達待ちまで move_to が面倒を見る
        await self.move_to({"lift_motor": "home", "arm_joint": "home"})

    @step("アーム展開", require_trigger=True)
    async def extend_arm(self):
        await self.move_to({"arm_joint": "extended"})

    # 失敗すると機構破損に直結する動作は全自動でも止める
    @step("ハンド閉じる", require_trigger=True, auto_stop=True)
    async def close_hand(self):
        await self.move_to({"gripper": "closed"})
```

### `Sequence.move_to` の責務

| 項目 | 決定 |
|---|---|
| 単位換算 | `PositionTable`（yaml の `axes`）が担当。シーケンスには数値が現れない |
| コート | `self.court` を自動で渡す。スカラー値の位置はコートに依存しない |
| 到達待ち | 軸ごとに `wait_reached(tolerance, timeout)` を並列実行 |
| タイムアウト | 軸ごとの `timeout_s`（既定 5.0s）。`move_to(..., timeout=)` で上書き可 |
| タイムアウト時 | `SequenceTimeoutError` を送出。`run()` が捕捉してログを残しシーケンスを停止 |
| 指令値の後始末 | **クリアしない**。昇降軸で保持トルクを失うとワークごと落下するため |

**タイムアウトで例外を投げる理由**: 黙って次のステップへ進むと、ワークを掴めていないのに
搬送に入る・アームが展開しきっていないのにハンドを閉じる、といった二次被害が出る。
`run()` は例外を握って `break` するので、シーケンスは停止して Web UI 上で止まった位置が分かり、
操縦者が原因を除去して該当ステップへジャンプできる。

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
| 5-1 | `lib/sequence/positions.py` | 機構位置定数の読み込み・単位換算・コート差異解決 |
| 5-2 | `lib/sequence/engine.py` | `bind_positions()` / `move_to()` / `SequenceTimeoutError` を追加 |
| 5-3 | `config/main_hand_positions.yaml` | メインハンドの位置定数（機構完成まで仮値） |
| 5-4 | `config/sub_hand_positions.yaml` | サブハンドの位置定数（機構完成まで仮値） |
| 5-5 | `robots/main_hand.py` | メインハンドのシーケンス（`move_to` で記述。数値は持たない） |
| 5-6 | `robots/sub_hand.py` | サブハンドのシーケンス（同上） |
| 5-7 | `main.py` | `<robot_name>_positions.yaml` を読んで `bind_positions()` |

**機構完成後にやること**:
1. `config/*_positions.yaml` の `axes.*.scale` / `offset` を実測値に置換
   （M3508 は `360 * (3591/187) / リード[mm/rev]`、EDULITE は deg→rad のまま）
2. `positions.*` の各値を実測ストロークに置換（現状は安全側の微小値）
3. `axes.*.timeout_s` を実動作時間 + 余裕に調整、必要なら `tolerance` を指定
4. コートで変わる位置があれば、その値だけ `{ red:, blue: }` に書き換え
5. `config/main_hand.yaml` の `motors.lift_motor.pid` を実機チューニング
6. `tests/test_robot_sequences.py` は位置名・軸名のみを検証しているので、
   値を変えてもテストは追随不要（軸／位置名を増減したときだけ更新）

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
  ├─ 0) RobotContext.position_loops を全て await pause()（0x200 の排他）
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
  └─ finally) position_loops を全て resume()（正常終了・abort・例外・キャンセルのいずれでも）
```

##### 安全策

- 動作確認シーケンス開始前に **確認ダイアログ必須**（「全モータを順番に微小駆動します。周囲の安全を確認してください」）
- 各モータの指令量は **物理的に安全な微小量に固定**（config で上書き可能）
- 動作確認実行中も **緊急停止コマンドは即時優先**（既存 e_stop 経路）
- M3508 の電流指令はリリース時に必ず 0 を再送（駆動状態を残さない）
- **M3508 位置制御ループとの排他**: 実行中は同ロボットの `M3508PositionLoop` を一時停止する。
  `RobotServer.add_robot(name, sequence, can_manager, position_loops=...)` でループを受け取り
  （`RobotContext.position_loops`。M3508 が居ない `sub_hand` は空リスト）、動作確認タスクの
  `try` 先頭で `pause()`、`finally` で `resume()` する。復帰漏れは保持電流の喪失（落下）に
  直結するため、`finally` での復帰は必須。
  排他はロボット単位で十分（`sub_hand` に M3508 は無く、両ロボットの M3508 バスは競合しない）

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

### Phase 8: モータアクセス層と M3508 位置制御 — TDD

Phase 5 で `move_to` の器はできたが、シーケンスから実モータへ指令が届く経路が無かった。
その配線と、M3508 に必要な PC 側位置ループ・緊急停止の穴埋めをまとめて行う。
詳細な仕様は「M3508 の位置制御（PC 側 PID）」「main.py での配線」を参照。

| # | ファイル | 内容 |
|---|---|---|
| 8-1 | `tests/test_sequence_motors.py` | **テスト先行**: 目標送信・到達待ち・緊急停止拒否・target_sink 差し込み |
| 8-2 | `lib/sequence/motors.py` | `MotorHandle` / `MotorGroup` / `build_motor_group` |
| 8-3 | `tests/drivers/test_target_reached.py` | **テスト先行**: 到達判定と許容差の既定値（全ドライバ横断） |
| 8-4 | `lib/drivers/base.py` (修正) | `is_target_reached()` / `default_tolerance()` / `_observed_for()` |
| 8-5 | `lib/sequence/engine.py` (修正) | `bind_motors()` / `wait_all_reached()` を追加し `move_to` を実指令に接続 |
| 8-6 | `tests/test_pid.py` | **テスト先行**: 比例/積分/微分、conditional integration、デッドバンド、出力制限 |
| 8-7 | `lib/control/pid.py` | モータ非依存 PID（測定値微分・積分ワインドアップ対策） |
| 8-8 | `tests/test_position_loop.py` | **テスト先行**: バス単位の 1 フレーム送信、dt 頭打ち、途絶時 0 電流、pause/resume、緊急停止 |
| 8-9 | `lib/control/position_loop.py` | `M3508PositionLoop` + `make_position_pid()` |
| 8-10 | `lib/drivers/m3508.py` (修正) | 多回転累積角（`multi_turn_position` / `reset_multi_turn_origin`）、`encode_current_frame`、減速比込みの許容差 |
| 8-11 | `tests/test_server_e_stop.py` | **テスト先行**: E-STOP でのシーケンス停止、コマンドゲート、解除後の復帰 |
| 8-12 | `lib/server.py` (修正) | E-STOP でシーケンス停止（送信失敗時も）、`_E_STOP_DENY_MESSAGES` ゲート、`e_stop_active` プロパティ、動作確認と位置制御ループの排他 |
| 8-13 | `tests/test_main_wiring.py` | **テスト先行**: PID の config 読み込み、バス単位ループ生成、両系統への緊急停止インターロック |
| 8-14 | `main.py` (修正) | `_load_pid_config` / `_build_position_pid` / `_build_position_loops` / `_wire_robot_motors` とライフサイクル |
| 8-15 | `config/main_hand.yaml` (修正) | `motors.lift_motor.pid` セクション（仮値） |

#### 設計上の判断

- **緊急停止インターロックは二重に置く**: シーケンス側（`MotorHandle.set_target` が
  `EStopActiveError`）と PID ループ側（出力を電流 0 に落とす）の両方。片方だけでは、
  既に走っているループ、または実行中ステップのどちらかが停止指令を上書きする
- **0x200 の排他はループを黙らせる方向**: 動作確認側を待たせるのではなく
  `pause()` / `resume()` でループを止める。`resume()` は同期メソッドなので `finally` から確実に呼べる
- **E-STOP はシーケンスも止める**: 停止フレームの送信可否に関わらず `finally` で停止する。
  シーケンスが走ったままだと次のステップが新しい目標値を送って停止を上書きする

---

### Phase 9: 励磁の有効化（EDULITE 05 の enable）— TDD

`initialization_steps()` に `encode_enable()` が無く、通常起動では EDULITE 05 が
無励磁のままだった（動作確認の `prepare_check_steps()` だけが enable していた）。
Phase 8 でシーケンスが実位置指令を出すようになったため、この穴を塞ぐ。

ただし **単に enable を足すのは危険**。本機は enable した瞬間に `PARAM_LOC_REF` へ
追従を始めるので、目標角が 0 のまま励磁するとアームが原点へ全速で飛ぶ。

| # | ファイル | 内容 |
|---|---|---|
| 9-1 | `tests/drivers/test_edulite05.py` | **テスト先行**: 現在角を書いてから enable する順序、位置モードのみフィードバック必須、速度モードは 0 保持、問い合わせフレームが disable であること、起動フレームに enable が混ざらないこと |
| 9-2 | `lib/drivers/base.py` (修正) | `activation_steps()` / `requires_fresh_feedback_for_activation()` / `feedback_probe_message()` の既定実装（既定はいずれも「有効化不要」） |
| 9-3 | `lib/drivers/edulite05.py` (修正) | 位置モードは実測角、それ以外は 0 を目標に書いてから `encode_enable()`。問い合わせは `encode_disable()` |
| 9-4 | `tests/test_can_manager.py` | **テスト先行**: 起動時に初期化 → 有効化の順で送ること、フィードバック受信後に目標を組み立てること、未受信なら有効化しないこと、中断できること |
| 9-5 | `lib/can_manager.py` (修正) | `activate_motor()` / `activate_motors()` / `_wait_fresh_feedback()` / `_send_steps()` |
| 9-6 | `tests/test_server_e_stop.py` | **テスト先行**: 緊急停止解除で再有効化が走ること、再度の緊急停止で中断できること |
| 9-7 | `lib/server.py` (修正) | `e_stop_release` から `_reactivate_motors()` |

#### 有効化シーケンス

```
initialization_steps()      disable → run_mode → limit_spd → limit_cur → loc_kp → (set_zero)
        ↓
_wait_fresh_feedback()      待機開始より "後に" 届いたフィードバックだけを認める
                            （応答が無いモータには disable を 50ms 周期で送って応答を促す）
        ↓
activation_steps()          LOC_REF = 実測角 → enable
```

#### 設計上の判断

- **enable は初期化フレームから分離する**: `initialization_steps()` は
  「送るだけで完結する純粋なフレーム列」のままにし、実測値に依存する有効化は
  `activation_steps()` として `CANManager` が適切なタイミングで組み立てる。
  この分離により「現在角を読んでから目標に書く」を型を変えずに表現できる
- **フィードバックは "待機開始より後に届いたもの" しか使わない**: `MotorState` の
  初期値 `position=0.0` を実測角と取り違えると原点へ飛ぶ。さらに
  `set_zero_on_start=true` では原点が付け替わるため、`set_zero` 以前に受信した値も
  実測角として使えない。`last_feedback_at` の値そのものではなく
  「待機開始後に更新されたか」を判定条件にすることで両方を同時に塞いでいる
- **確認できないときは有効化しない**: フィードバックが得られなければ enable を送らず
  無励磁のまま残す（`activate_motor()` は False を返し WARNING を出す）。
  「動かない」より「意図せず飛ぶ」ほうが危険なので、安全側は無励磁側にある
- **問い合わせは `disable`**: 無励磁を保ったまま応答だけ得られる唯一のフレーム。
  `clear_fault=False` なので障害フラグを握り潰さない
- **緊急停止解除で再有効化する**: 解除は `_e_stop_active = False` にするだけでは
  EDULITE が無励磁のままで以後の指令が一切効かない。解除時に `activate_motors()` を
  呼ぶが、有効化自体が「現在角を保持目標に書いてから」なので解除操作で機体は動かない。
  再有効化の途中で緊急停止が再度入った場合に備え、`should_abort` で中断し、
  それでも通り抜けた enable のために停止フレームを再送する

---

## 未解決の課題

実装済みだが実機・運用面で未対応の項目。競技当日までに潰すか、意識的に許容するかを決める必要がある。

### 安全系

| 課題 | 現状 | 影響 |
|---|---|---|
| `_e_stop_active` がプロセスメモリ上のみ | `RobotServer.__init__` で `False` に初期化されるだけで永続化しない | サーバーを再起動すると緊急停止状態が消える。物理的な緊急停止ボタンの状態と同期する仕組みも無く、UI 上「解除済み」に見えるまま実機は停止しているという不一致が起きうる |
| 緊急停止で fault がラッチされた場合の復帰手順が無い | `e_stop_release` は Phase 9 で `activate_motors()`（現在角を書いてから enable）を呼ぶようになったが、`encode_disable(clear_fault=True)` は送らない | EDULITE 05 が過電流等の障害フラグを保持したままだと、再有効化しても指令が効かない。fault の自動クリアは原因を隠すため意図的に行っていない。実機で「解除しても動かない」場合は fault の内容を確認して電源再投入で対処する（`health` の `FAULT` 表示で判別できる） |
| フィードバックが得られないと EDULITE が無励磁のまま残る | Phase 9 の `activate_motor()` は待機（既定 0.5s）の間にフィードバックを受け取れないと enable を送らず、WARNING をログに出すだけ | 電源断・配線ミス・CAN 断のときは「シーケンスは進むのに軸だけ動かない」状態になる。ログを見ないと気づけないので、有効化を見送ったモータを UI（health / 起動時バナー）に出す仕組みが欲しい。なお `--dry-run` は virtual バスで応答が無いため、この WARNING が必ず 2 件出るのが正常 |
| M3508 のホーミングが未実装 | `multi_turn_position` の原点は初回フィードバック受信時の姿勢。`reset_multi_turn_origin()` / `M3508PositionLoop.set_origin_here()` を呼ぶ経路がシーケンスにも `main.py` にも無い | 「目標 0 = 電源投入時の位置」であり機械原点ではない。電源投入時の姿勢が毎回違うと `positions` の値がそのままズレる。機構端への押し当て（`ControlMode.CURRENT` の素通し）は用意してあるが、それを使うステップがまだ無い |
| `_receive_loop` の例外が握りつぶされる | 「既知の制約: バス down 時の失敗が分かりにくい」参照 | 対策候補は同節に記載済み。未着手 |

### 制御・チューニング

| 課題 | 現状 | 影響 |
|---|---|---|
| PID ゲイン・機構定数がすべて仮値 | `main.py` の `_DEFAULT_PID`（kp=2.0 / ki=kd=0 / dead_band=1.0 / output_limit=2000）、`config/*_positions.yaml` の `scale` / `offset` / `positions` はいずれも安全側に振った仮値 | **実機チューニング必須**。現状のゲインでは重力負荷を持ち上げられない可能性が高い（ki=0 のため定常偏差が残る） |
| M3508 の位置制御が実機未検証 | 多回転アンラップ・PID・到達判定とも単体テストのみ | ラップアラウンド判定のしきい値（半周＝3600rpm 相当）や減速比込みの許容差が実機で妥当かは未確認 |
| 200Hz が実機で維持できるか未測定 | 周期の実測・ジッタのロギング機構が無い | 「asyncio で 200Hz の位置ループを回す判断」の限界節を参照。乱れた場合の備えはあるが、乱れているかどうかを知る手段が無い |
| `dead_band=1.0`（モータ軸 deg）と `default_tolerance` の関係 | 到達判定の許容差は出力軸 1deg 相当（モータ軸で約 19.2deg）、PID のデッドバンドはモータ軸 1deg | 現状は許容差 > デッドバンドなので到達はするが、チューニングで両者を動かすときは大小関係を意識する必要がある（デッドバンドが許容差より広いと永久に到達しない） |

### 運用

| 課題 | 現状 | 影響 |
|---|---|---|
| `set_param` が未実装 | `lib/server.py` はログ出力のみ | PID Tuning タブから実機のゲインを変更できない。緊急停止中のゲートだけは先に入れてある |
| 位置定数の実機反映手順が手動 | 「Phase 5 > 機構完成後にやること」参照 | 未着手 |

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
